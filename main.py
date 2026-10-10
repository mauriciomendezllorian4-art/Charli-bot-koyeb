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

# --- FUNCIONES TUYAS, NO LAS CAMBIO, SOLO ARREGLO LO QUE FALLA ---
def get_precio_oro():
    try:
        url = f"https://api.twelvedata.com/price?symbol=XAU/USD&apikey={TD_KEY}"
        data = requests.get(url, timeout=10).json()
        print(f"TwelveData ORO: {data}")
        if "price" in data:
            return data["price"]
        return None
    except Exception as e:
        print(f"Error TwelveData: {e}")
        return None

def preguntar_a_ia(mensaje_usuario):
    # Esta es tu funcion de IA, la dejo igual, solo cambio el modelo viejo
    respuesta = client.chat.completions.create(
        model="llama-3.1-8b-instant", # ESTE ERA EL ERROR, EL VIEJO YA NO EXISTE
        messages=[
            {"role": "system", "content": "Sos Charli, un experto en finanzas, oro y dolar. Respondé corto, en español argentino, claro y directo."},
            {"role": "user", "content": mensaje_usuario}
        ]
    )
    return respuesta.choices[0].message.content

@app.route("/")
def home():
    return "Charli bot vivo!"

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json()
        print(f"DATA: {data}")

        if "message" in data and "text" in data["message"]:
            chat_id = data["message"]["chat"]["id"]
            text = data["message"]["text"]
            text_lower = text.lower()

            # Comando /oro
            if "/oro" in text_lower or text_lower.startswith("oro"):
                precio = get_precio_oro()
                if not precio:
                    texto_respuesta = "No pude traer el precio del oro ahora, probá en 1 minuto."
                else:
                    prompt = f"El precio del oro es {precio} USD la onza. Explicá en 3 lineas que significa hoy para alguien en Argentina."
                    texto_respuesta = preguntar_a_ia(prompt)
            else:
                # Cualquier otro mensaje va a la IA, tu funcion normal
                texto_respuesta = preguntar_a_ia(text)

            requests.post(f"{API_TELEGRAM}/sendMessage", json={"chat_id": chat_id, "text": texto_respuesta})

    except Exception as e:
        print(f"ERROR REAL WEBHOOK: {e}")
        # Para que veas el error real en Telegram tambien
        try:
            chat_id = data["message"]["chat"]["id"]
            requests.post(f"{API_TELEGRAM}/sendMessage", json={"chat_id": chat_id, "text": f"Error: {e}"})
        except:
            pass

    return "ok", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
    



   
           
                
                
      
   
    
   
    


 
    
      
       
  
  
