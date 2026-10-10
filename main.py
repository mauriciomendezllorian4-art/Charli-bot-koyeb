import os
import requests
from flask import Flask, request
from groq import Groq

app = Flask(__name__)

TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_KEY = os.environ.get("GROQ_API_KEY")
TD_KEY = os.environ.get("TWELVEDATA_KEY")

API_TELEGRAM = f"https://api.telegram.org/bot{TOKEN}"
client = Groq(api_key=GROQ_KEY)

def get_precio_oro():
    try:
        url = f"https://api.twelvedata.com/price?symbol=XAU/USD&apikey={TD_KEY}"
        data = requests.get(url, timeout=10).json()
        print(f"TwelveData: {data}")
        if "price" in data:
            return data["price"]
        return None
    except Exception as e:
        print(f"Error oro: {e}")
        return None

def preguntar_a_ia(mensaje):
    # MODELO NUEVO QUE SI FUNCIONA EN FREE
    resp = client.chat.completions.create(
        model="openai/gpt-oss-20b",
        messages=[
            {"role": "system", "content": "Sos Charli, experto en oro, dolar y finanzas para Argentina. Respondé corto y claro en español argentino."},
            {"role": "user", "content": mensaje}
        ]
    )
    return resp.choices[0].message.content

@app.route("/")
def home():
    return "Charli vivo!"

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json()
        print(f"Webhook: {data}")

        if "message" in data and "text" in data["message"]:
            chat_id = data["message"]["chat"]["id"]
            text = data["message"]["text"]
            text_lower = text.lower()

            if "/oro" in text_lower or text_lower.startswith("oro"):
                precio = get_precio_oro()
                if not precio:
                    respuesta = "No pude traer el oro ahora, probá en 1 min."
                else:
                    respuesta = preguntar_a_ia(f"El oro está {precio} USD. Explicá en 3 líneas que significa hoy.")
            else:
                respuesta = preguntar_a_ia(text)

            requests.post(f"{API_TELEGRAM}/sendMessage", json={"chat_id": chat_id, "text": respuesta})

    except Exception as e:
        print(f"ERROR REAL: {e}")
        try:
            chat_id = data["message"]["chat"]["id"]
            requests.post(f"{API_TELEGRAM}/sendMessage", json={"chat_id": chat_id, "text": f"Error: {e}"})
        except:
            pass

    return "ok", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)

 
    
      
       
  
  
