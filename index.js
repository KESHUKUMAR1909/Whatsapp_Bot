require('dotenv').config();
const express = require('express');
const bodyParser = require('body-parser');
const cors = require('cors');
const twilio = require('twilio');

const app = express();
const port = process.env.PORT || 3000;

// Middleware
app.use(cors());
app.use(bodyParser.json());
app.use(bodyParser.urlencoded({ extended: true }));

// Twilio Client
const client = twilio(process.env.TWILIO_ACCOUNT_SID, process.env.TWILIO_AUTH_TOKEN);

// Routes
app.get('/', (req, res) => {
    res.send('Namandarshan WhatsApp Backend is running!');
});

/**
 * Endpoint to send WhatsApp message
 * Body: { to: 'whatsapp:+91...', contentSid: '...', contentVariables: { ... } }
 */
app.post('/send-whatsapp', async (req, res) => {
    const { to, contentSid, contentVariables } = req.body;

    if (!to || !contentSid) {
        return res.status(400).json({ error: 'Missing required parameters: to and contentSid are required.' });
    }

    try {
        const message = await client.messages.create({
            from: process.env.TWILIO_PHONE_NUMBER,
            to: to,
            contentSid: contentSid,
            contentVariables: JSON.stringify(contentVariables || {})
        });

        console.log(`Message sent! SID: ${message.sid}`);
        res.status(200).json({ success: true, messageSid: message.sid });
    } catch (error) {
        console.error('Error sending WhatsApp message:', error.message, error.code, error.moreInfo);
        res.status(500).json({ success: false, error: error.message, code: error.code });
    }
});

// Start Server
app.listen(port, () => {
    console.log(`Server is listening at http://localhost:${port}`);
});
