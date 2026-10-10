from flask import Flask, request
import os

app = Flask(__name__)

@app.route('/')
def home():
    return "CHALI V17 SALTA ONLINE - OK", 200

@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        # Verificacion de Meta
        mode = request.args.get('hub.mode')
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')
        if mode == 'subscribe' and token == 'CHALI123':
            return challenge, 200
        return 'Forbidden', 403
    else:
        # Aqui va tu logica del bot
        print(request.json)
        return 'OK', 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

            
       
  
