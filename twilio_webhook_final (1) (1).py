import os
import logging
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from groq import Groq
from pymongo import MongoClient
from datetime import datetime
from dotenv import load_dotenv
import re
from twilio.rest import Client

# Load environment variables
load_dotenv()

# Logging Configuration
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# --- Configuration & Clients ---
class GroqManager:
    """Handles rotation between multiple Groq API keys to manage rate limits."""
    def __init__(self):
        self.keys = [
            os.getenv("GROQ_API_KEY_1"),
            os.getenv("GROQ_API_KEY_2"),
            os.getenv("GROQ_API_KEY_3"),
            os.getenv("GROQ_API_KEY_4")
        ]
        self.current_index = 0
        self.clients = [Groq(api_key=key) for key in self.keys if key]
        if not self.clients:
            raise ValueError("No Groq API keys found in .env")

    def get_client(self):
        client = self.clients[self.current_index]
        # Rotate for next call
        self.current_index = (self.current_index + 1) % len(self.clients)
        return client

class ServiceManager:
    """Handles MongoDB operations for temples, leads, and chat history."""
    def __init__(self):
        source_uri = os.getenv("SOURCE_MONGODB_URI")
        target_uri = os.getenv("TARGET_MONGODB_URI")
        
        self.source_client = MongoClient(source_uri)
        self.target_client = MongoClient(target_uri)
        
        # Source DB (Temples & Leads)
        self.source_db = self.source_client.get_database("temples")
        self.temples_col = self.source_db.get_collection("temples")
        self.leads_col = self.source_db.get_collection("leads")
        
        # Target DB (AI/Chat Logs)
        self.target_db = self.target_client.get_database("WhatsappNamandarshan")
        self.history_col = self.target_db.get_collection("chat_history")
        self.refinement_col = self.target_db.get_collection("refinement_qa")
        self.leads_whatsapp_col = self.target_db.get_collection("lead_from_whatsapp")

    def search_temples(self, query):
        """Search for temples based on a keyword query."""
        # Simple regex search on name or location
        regex = re.compile(query, re.IGNORECASE)
        results = list(self.temples_col.find({
            "$or": [
                {"name": regex},
                {"location": regex},
                {"deity": regex}
            ]
        }).limit(3))
        return results

    def save_lead(self, user_id, user_name, service_type, temple_name):
        """Save a booking lead to the database."""
        lead = {
            "userId": user_id,
            "name": user_name,
            "service": service_type,
            "temple": temple_name,
            "source": "whatsapp",
            "timestamp": datetime.now(),
            "status": "new"
        }
        self.leads_col.insert_one(lead)
        return lead

    def get_context(self, user_id, limit=5):
        """Retrieve recent chat history for context."""
        history = list(self.history_col.find({"userId": user_id}).sort("timestamp", -1).limit(limit))
        # Reverse to get chronological order
        return history[::-1]

    def save_interaction(self, user_id, user_msg, bot_res, intent):
        """Save a user-bot interaction."""
        interaction = {
            "userId": user_id,
            "user_message": user_msg,
            "bot_response": bot_res,
            "intent": intent,
            "timestamp": datetime.now()
        }
        self.history_col.insert_one(interaction)

    def initiate_whatsapp_lead(self, user_id, initial_msg):
        """Start a new lead record for a WhatsApp user."""
        lead = {
            "userId": user_id,
            "messages": [initial_msg],
            "timestamp": datetime.now(),
            "status": "capturing"
        }
        return self.leads_whatsapp_col.insert_one(lead).inserted_id

    def get_active_whatsapp_lead(self, user_id):
        """Get the most recent active lead for a user that is still capturing context."""
        return self.leads_whatsapp_col.find_one({
            "userId": user_id,
            "status": "capturing"
        }, sort=[("timestamp", -1)])

    def append_to_whatsapp_lead(self, lead_id, message):
        """Append a message to an existing lead and update status if limit reached."""
        lead = self.leads_whatsapp_col.find_one({"_id": lead_id})
        if not lead:
            return
        
        messages = lead.get("messages", [])
        messages.append(message)
        
        status = "capturing"
        if len(messages) >= 6: # Initial + 5 next messages
            status = "complete"
            
        self.leads_whatsapp_col.update_one(
            {"_id": lead_id},
            {"$set": {"messages": messages, "status": status}}
        )

groq_manager = GroqManager()
service_manager = ServiceManager()

# Twilio Client Initialization
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")

if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER]):
    logger.warning("Twilio credentials or phone number missing in .env. REST API sending will fail.")

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# --- AI Logic ---

SYSTEM_PROMPT = """
You are the Namandarshan AI Assistant, a professional and helpful guide for temple services in India.
Your goal is to assist users with information about temples, pujas, Darshan bookings, and other spiritual services.

CRITICAL RULE:
- DO NOT INCLUDE ANY PRICING DETAILS (₹, Rupees, Cost, Free, etc.) in your response.
- If a user asks about price, say: "Our Namandarshan team will provide you with all pricing and availability details when they contact you shortly."

CORE SERVICES:
1. Darshan Booking (VIP/General)
2. Puja Booking (Online/Offline)
3. Prasadam Delivery
4. Astrology & Kundli
5. Yatra Packages (Chardham, Kashi, etc.)

DOMAIN BOUNDARIES:
- Strictly answer only about Namandarshan and its services.
- If a user asks about unrelated topics (cricket, politics, general knowledge), politely decline.
- Handle English, Hindi, and Hinglish (Hindi written in English script) naturally.

CONVERSION STRATEGY:
- After providing temple info, gently nudge the user to book a service if applicable.
- For bookings, notify the user that "I've initiated the booking process for you. Our Namandarshan team will contact you shortly to confirm your booking and provide more information."

IDENTITIES:
- Name: Namandarshan AI Bot
- Purpose: Help devotees connect with temples and spiritual services.
"""

def generate_ai_response(user_id, message, reset_context=False):
    client = groq_manager.get_client()
    
    # Get context (unless reset)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    
    if not reset_context:
        history = service_manager.get_context(user_id)
        for h in history:
            messages.append({"role": "user", "content": h.get('user_message', h.get('userMessage', ''))})
            messages.append({"role": "assistant", "content": h.get('bot_response', h.get('botResponse', ''))})
    
    messages.append({"role": "user", "content": message})

    print(f"\n--- AI Processing for User {user_id} ---")
    print(f"User Message: {message}")

    try:
        completion = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            temperature=0.4, 
            max_tokens=1024,
            timeout=15.0 # Add timeout to prevent hangs
        )
        response = completion.choices[0].message.content
        print(f"[AI] Response: {response[:150]}...")
        return response
    except Exception as e:
        logger.error(f"Groq API Error: {e}")
        return "I'm having a bit of trouble connecting to my brain right now. Please try again in a moment! 🙏"

def classify_intent(message):
    """Simple rule-based classification or can be AI-based. 
    For better accuracy, we use a lightweight Groq call for intent."""
    client = groq_manager.get_client()
    prompt = f"Classify the intent of this WhatsApp message: '{message}'. Categories: [info, lead, other]. Just output the category name."
    try:
        completion = client.chat.completions.create(
            model="llama-3.1-8b-instant", # Use a cheaper model for intent
            messages=[{"role": "user", "content": prompt}]
        )
        return completion.choices[0].message.content.lower().strip()
    except:
        return "info"

# --- Webhook Route ---

@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    incoming_msg = request.values.get('Body', '').strip()
    sender_id = request.values.get('From', '')
    sender_name = request.values.get('ProfileName', 'User')

    print(f"\n>>> Incoming Webhook from {sender_id} ({sender_name})")
    print(f">>> Message: {incoming_msg}")
    
    logger.info(f"Received message from {sender_id}: {incoming_msg}")

    # 1. Check for Greeting Reset
    reset_context = False
    if incoming_msg.lower() in ["hi", "hello", "namaste"]:
        reset_context = True

    # 2. Classify Intent
    intent = classify_intent(incoming_msg)
    
    # 3. Handle Active Lead State (Capture next 5 messages)
    active_lead = service_manager.get_active_whatsapp_lead(sender_id)
    if active_lead:
        service_manager.append_to_whatsapp_lead(active_lead["_id"], incoming_msg)
    elif intent == 'lead':
        # Start new lead capture
        service_manager.initiate_whatsapp_lead(sender_id, incoming_msg)

    # 4. Extract context-specific info if intent is 'info'
    extra_context = ""
    if intent == 'info':
        keywords = incoming_msg.split()
        for word in keywords:
            if len(word) > 3:
                results = service_manager.search_temples(word)
                if results:
                    extra_context = f"\nDatabase Info: {results[0]}"
                    break

    # 5. Generate AI Response
    final_message = incoming_msg + extra_context
    response_text = generate_ai_response(sender_id, final_message, reset_context=reset_context)

    # 6. Save Interaction History
    service_manager.save_interaction(sender_id, incoming_msg, response_text, intent)

    # 7. Respond via Twilio REST API
    # try:
    #     message = twilio_client.messages.create(
    #         body=response_text,
    #         from_=TWILIO_PHONE_NUMBER,
    #         to=sender_id
    #     )
    #     logger.info(f"Message sent to {sender_id}. SID: {message.sid}")
    # except Exception as e:
    #     logger.error(f"Twilio REST API Error: {e}")
    #     # Fallback to TwiML if REST API fails (optional, but REST is usually better)
    #     resp = MessagingResponse()
    #     resp.message(response_text)
    #     return str(resp), 200, {'Content-Type': 'application/xml'}

    # # 8. Acknowledge Twilio with empty TwiML
    # return '<?xml version="1.0" encoding="UTF-8"?><Response></Response>', 200, {'Content-Type': 'application/xml'}

if __name__ == "__main__":
    # Disable debug mode to avoid WinError 10038 in some environments
    app.run(port=5002, debug=False)
