const axios = require('axios');

const testWhatsApp = async () => {
    try {
        const response = await axios.post('http://localhost:3000/send-whatsapp', {
            to: 'whatsapp:+919340179767',
            contentSid: 'HXb5b62575e6e4ff6129ad7c8efe1f983e',
            contentVariables: {
                "1": "12/1",
                "2": "3pm"
            }
        });

        console.log('Success:', response.data);
    } catch (error) {
        if (error.response) {
            console.error('Error:', error.response.data);
        } else {
            console.error('Error:', error.message);
        }
    }
};

testWhatsApp();
