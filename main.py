from flask import Flask, request
import os

app = Flask(__name__)

@app.route('/')
def home():
    return "CHALI V17 SALTA - ONLINE", 200

@app.route('/webhook', methods=['GET','POST'])
def webhook():
    if request.method == 'GET':
        if request.args.get('hub.mode') == 'subscribe' and request.args.get('hub.verify_token') == 'CHALI123':
            return request.args.get('hub.challenge'), 200
        return 'Error', 403
    # POST
    data = request.get_json()
    print(data)
    return 'OK', 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

       
    

            
       
  
