# -*- coding: utf-8 -*-
"""
占い店専用LINE自動ボット - Flask メインアプリケーション (Renderデプロイ対応版)
Gemini APIを使用した自動占いサービス
会話ログをコンソールに出力し、Renderのログ機能で確認可能にする
"""


import os
import json
import hashlib
import logging
from datetime import datetime


from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import google.generativeai as genai


# ===========================
# ロギング設定
# ===========================
# Renderのログは標準出力/エラーに出力されるため、基本的な設定で十分
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ===========================
# Flask アプリケーション初期化
# ===========================
app = Flask(__name__)


# ===========================
# 環境変数から設定を読み込む
# ===========================
LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')
LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
SHOP_NAME = os.getenv('SHOP_NAME', '黒猫の占い館')
RESERVATION_URL = os.getenv('RESERVATION_URL', 'https://example.com/reserve')


# 環境変数の検証 (アプリケーション起動時にログで警告)
if not all([LINE_CHANNEL_SECRET, LINE_CHANNEL_ACCESS_TOKEN, GEMINI_API_KEY]):
    logger.error('【重要】必要な環境変数が設定されていません。LINE_CHANNEL_SECRET, LINE_CHANNEL_ACCESS_TOKEN, GEMINI_API_KEY をRenderの環境変数に設定してください。')


# ===========================
# LINE Bot SDK 初期化
# ===========================
# 環境変数がNoneの場合でもエラーにならないように、空文字を渡す
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN or '')
webhook_handler = WebhookHandler(LINE_CHANNEL_SECRET or '')


# ===========================
# Gemini API 初期化
# ===========================
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)


# ===========================
# システムプロンプト定義
# ===========================
SYSTEM_PROMPT = f"""あなたは占い店「{SHOP_NAME}」の「黒猫のルナ」です。
ユーザーの相談に対し、タロットカードの大アルカナの概念を用いて、神秘的かつ親しみやすい口調（「〜だニャ」「〜という暗示が出ているわ」）でアドバイスしてください。
ただし、ユーザーの文章に以下の深刻なキーワードが含まれる場合は、
「その悩みは私の魔力では見通せない深い霧の中にあるわ。お店にいる先生たちに直接相談することをお勧めするニャ。{RESERVATION_URL}」
と回答し、店舗への来店を促してください。
深刻なキーワード：
- 不倫、浮気、離婚
- 死、自殺、自傷
- 借金、多重債務、詐欺
- 裁判、逮捕、犯罪
- 重大な病気、精神疾患
回答は必ず100文字以上300文字以内で、親しみやすく、かつ占いらしい神秘的な表現を心がけてください。"""


CRITICAL_KEYWORDS = ['不倫', '浮気', '離婚', '死', '自殺', '自傷', '借金', '多重債務', '詐欺', '裁判', '逮捕', '犯罪', '重大な病気', '精神疾患', 'うつ', 'パニック']
CATEGORY_KEYWORDS = {
    '恋愛': ['恋', '好き', '彼氏', '彼女', '付き合い', '告白', '結婚', '婚活', 'デート'],
    '仕事': ['仕事', '職場', '上司', '同僚', '転職', 'キャリア', '昇進', '給与', '退職'],
    '家庭': ['家族', '親', '子ども', '夫', '妻', '兄弟', '姉妹', '親戚', '嫁'],
    '人間関係': ['友人', '友達', '人間関係', 'いじめ', 'トラブル', '喧嘩'],
    '金銭': ['お金', '貯金', '投資', '浪費', '給与', '昇給'],
    '健康': ['健康', '病気', '体調', 'ストレス', '疲労', '睡眠'],
    'その他': []
}


# ===========================
# ユーティリティ関数
# ===========================
def hash_user_id(user_id: str) -> str:
    return hashlib.sha256(user_id.encode()).hexdigest()[:16]


def estimate_category(text: str) -> str:
    text_lower = text.lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(keyword in text_lower for keyword in keywords):
            return category
    return 'その他'


def contains_critical_keywords(text: str) -> bool:
    return any(keyword in text for keyword in CRITICAL_KEYWORDS)


def summarize_concern(text: str, max_length: int = 50) -> str:
    return text[:max_length] + '...' if len(text) > max_length else text


# ★★★ ログ保存方法をコンソール出力に変更 ★★★
def save_conversation_log(user_id: str, user_message: str, bot_response: str, category: str, is_critical: bool) -> None:
    """会話ログをコンソールにJSON形式で出力する (Renderのログ機能用)"""
    try:
        log_data = {
            'timestamp': datetime.now().isoformat(),
            'user_id_hash': hash_user_id(user_id),
            'category': category,
            'concern_summary': summarize_concern(user_message),
            'user_message': user_message,
            'bot_response': bot_response,
            'is_critical': is_critical
        }
        # ログのヘッダーを付けて、Renderのログ検索でフィルタリングしやすくする
        # ensure_ascii=Falseで日本語が文字化けしないようにする
        print(f"[CONVERSATION_LOG] {json.dumps(log_data, ensure_ascii=False)}")
    except Exception as e:
        logger.error(f'ログ出力エラー: {str(e)}')


def get_gemini_response(user_message: str) -> str:
    """Gemini APIから占いの回答を取得する"""
    if not GEMINI_API_KEY:
        logger.error('Gemini APIキーが設定されていません。')
        return 'ごめんなさい、魔力の源が見つからないニャ…。設定を確認してほしいニャ。'
    try:
        # ★★★ モデル名を gemini-1.5-flash に変更 ★★★
        model = genai.GenerativeModel('gemini-1.5-flash')
        # system_instructionをユーザーメッセージに統合
        full_prompt = f"{SYSTEM_PROMPT}\n\nユーザーからの相談: {user_message}"
        response = model.generate_content(full_prompt, generation_config=genai.types.GenerationConfig(temperature=0.9, top_p=0.95, max_output_tokens=500))
        
        # レスポンスの検証
        if not response or not response.candidates:
            logger.error('Gemini API: 応答が空です')
            return 'すみません、今は魔力が弱まっているようです。少し時間をおいてからもう一度お試しください。ニャ〜'
        
        # テキストを安全に取得
        if hasattr(response, 'text') and response.text:
            return response.text.strip()
        elif response.candidates and len(response.candidates) > 0:
            candidate = response.candidates[0]
            if hasattr(candidate, 'content') and hasattr(candidate.content, 'parts') and candidate.content.parts:
                return candidate.content.parts[0].text.strip()
        
        logger.error('Gemini API: テキストを抽出できませんでした')
        return 'すみません、今は魔力が弱まっているようです。少し時間をおいてからもう一度お試しください。ニャ〜'
    except Exception as e:
        logger.error(f'Gemini API エラー: {str(e)}')
        return 'すみません、今は魔力が弱まっているようです。少し時間をおいてからもう一度お試しください。ニャ〜'


def handle_user_message(user_id: str, user_message: str) -> str:
    """ユーザーメッセージを処理し、ボット応答を生成する"""
    category = estimate_category(user_message)
    is_critical = contains_critical_keywords(user_message)
    
    if is_critical:
        bot_response = f'その悩みは私の魔力では見通せない深い霧の中にあるわ。お店にいる先生たちに直接相談することをお勧めするニャ。{RESERVATION_URL}'
    else:
        bot_response = get_gemini_response(user_message)
    
    save_conversation_log(user_id, user_message, bot_response, category, is_critical)
    return bot_response


# ===========================
# Flask ルート定義
# ===========================
@app.route('/callback', methods=['POST'])
def callback():
    """LINE Webhookのコールバック処理"""
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    
    try:
        webhook_handler.handle(body, signature)
    except InvalidSignatureError:
        logger.error('無効な署名です。LINE_CHANNEL_SECRETを確認してください。')
        abort(400)
    
    return 'OK'


@webhook_handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    """テキストメッセージの処理"""
    user_id = event.source.user_id
    user_message = event.message.text
    
    logger.info(f'ユーザー {hash_user_id(user_id)} からのメッセージ: {user_message}')
    
    bot_response = handle_user_message(user_id, user_message)
    
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=bot_response)
    )


@app.route('/')
def index():
    """ヘルスチェック用エンドポイント"""
    return 'LINE Bot is running!'


# ===========================
# アプリケーション起動
# ===========================
if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
