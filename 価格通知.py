from flask import Flask, request, jsonify
import requests
from apscheduler.schedulers.background import BackgroundScheduler
import threading

# APIキーを直接埋め込む
LINE_ACCESS_TOKEN = "l3u20zZrzT9jjO2nvwmFhhFH4tQPaZy/fr5FYckTQnQos6eY0GYAA+tWVGMdMCpa28KJC9ck6GFKx46LXhrBK/mbJD3fo+mnDA4HQGkYI2E9wimR1v1Z7MKJ7RiWkE+1H77KjNM2kFYGtsaH4yJ3fQdB04t89/1O/w1cDnyilFU="
LINE_SECRET = "865458e81cc1f3f6950a50a6ba6a20a4"
BITBANK_API_KEY = "ac6d6edb914364075d74bb1df11ae12c33c5ae634dbf7326fed5e9b5c9610c11"

# Flaskアプリケーション
app = Flask(__name__)

# ユーザーごとの監視リスト
watch_list = {}
user_state = {}

# 固定の通貨ペア
available_pairs = {
    "BTC": "btc_jpy",
    "XRP": "xrp_jpy",
    "ETH": "eth_jpy",
    "FLR": "flr_jpy",  # フレア (Flare) 取引所で取引されているペアを仮定
    "XLM": "xlm_jpy"   # ステラ (Stellar) 取引所で取引されているペアを仮定
}

# ビットバンクAPIで価格を取得する関数
def get_crypto_price(pair="btc_jpy"):
    url = f"https://public.bitbank.cc/{pair}/ticker"
    response = requests.get(url)
    if response.status_code == 200:
        data = response.json()
        return float(data['data']['last'])  # 現在価格
    return None

# LINEメッセージ送信
def send_line_message(user_id, message):
    headers = {
        "Authorization": f"Bearer {LINE_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "to": user_id,
        "messages": [{"type": "text", "text": message}]
    }
    response = requests.post("https://api.line.me/v2/bot/message/push", headers=headers, json=data)
    if response.status_code != 200:
        print(f"Failed to send message: {response.text}")
    return response.status_code

# ユーザーの価格監視リストをチェック
def check_prices():
    for user_id, settings in list(watch_list.items()):
        pair = settings['pair']
        target_price = settings['target_price']
        current_price = get_crypto_price(pair)
        if current_price and current_price >= target_price:
            send_line_message(user_id, f"{pair.upper()}が指定価格 {target_price} JPY に達しました！ 現在価格: {current_price} JPY")
            del watch_list[user_id]  # 通知後に削除

# LINE Webhook
@app.route("/callback", methods=["POST"])
def callback():
    body = request.json
    events = body.get("events", [])
    for event in events:
        if event["type"] == "message" and event["message"]["type"] == "text":
            user_id = event["source"]["userId"]
            message_text = event["message"]["text"]

            # ユーザーの状態確認
            if user_id in user_state:
                # ユーザーが価格を入力するフェーズ
                try:
                    target_price = float(message_text)
                    pair = user_state[user_id]
                    watch_list[user_id] = {"pair": pair, "target_price": target_price}
                    send_line_message(user_id, f"{pair.upper()}の価格を {target_price} JPY で監視します。")
                    del user_state[user_id]  # 状態をリセット
                except ValueError:
                    send_line_message(user_id, "価格は数値で入力してください。")
            else:
                # 通貨選択を促す
                if message_text.lower() == "start":
                    pair_list = "\n".join([f"{name} ({pair})" for name, pair in available_pairs.items()])
                    send_line_message(user_id, f"以下の通貨ペアから選択してください:\n{pair_list}")
                elif message_text.upper() in available_pairs:
                    user_state[user_id] = available_pairs[message_text.upper()]
                    send_line_message(user_id, f"{message_text.upper()}を選択しました。監視したい価格を入力してください。")
                else:
                    send_line_message(user_id, "無効なコマンドです。「start」と入力して始めてください。")
    return jsonify({"status": "ok"})

# スケジューラー設定
scheduler = BackgroundScheduler()
scheduler.add_job(check_prices, "interval", seconds=30)  # 30秒ごとに価格をチェック
scheduler.start()

# Flaskアプリを別スレッドで実行
if __name__ == "__main__":
    threading.Thread(target=lambda: app.run(port=5000, use_reloader=False)).start()