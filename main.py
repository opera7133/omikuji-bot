import discord
from discord.ext import commands
import secrets
import sqlite3
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
load_dotenv()

# import google.generativeai as genai

# GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
# genai.configure(api_key=GOOGLE_API_KEY)
# gemini = genai.GenerativeModel("gemini-1.5-flash-002")

# --- おみくじの種類とテキスト ---
fortunes = {
    "大吉": "最高の一日になるでしょう！すべてがうまくいく予感。",
    "中吉": "良いことがありそう。前向きに行動してみて！",
    "小吉": "小さな幸せが訪れるかも。些細なことに感謝を。",
    "吉": "安定した一日。落ち着いて行動すると◎",
    "末吉": "控えめな行動が吉。焦らずじっくりと。",
    "凶": "今日は慎重に。無理せず休むのも大事です。"
}

# --- SQLiteの初期化 ---
conn = sqlite3.connect("omikuji.db")
c = conn.cursor()

# 抽選履歴テーブル
c.execute('''
    CREATE TABLE IF NOT EXISTS draws (
        user_id INTEGER,
        draw_date TEXT,
        fortune TEXT
    )
''')

# 運勢の統計テーブル
c.execute('''
    CREATE TABLE IF NOT EXISTS stats (
        fortune TEXT PRIMARY KEY,
        count INTEGER
    )
''')

# 初期データを挿入（存在しない運勢だけ）
for fortune in fortunes:
    c.execute(
        "INSERT OR IGNORE INTO stats (fortune, count) VALUES (?, ?)", (fortune, 0))

conn.commit()

# --- Botのセットアップ ---
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# スラッシュコマンドの登録


@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f'Logged in as {bot.user.name}')


# --- /omikuji コマンド ---
@bot.tree.command(name="omikuji", description="今日の運勢を占います（1日1回）")
async def omikuji(interaction: discord.Interaction):
    user_id = interaction.user.id
    today = datetime.now(ZoneInfo("Asia/Tokyo")).date().isoformat()

    # 既に今日引いたか確認
    c.execute(
        "SELECT * FROM draws WHERE user_id = ? AND draw_date = ?", (user_id, today))
    if c.fetchone():
        await interaction.response.send_message("おみくじは1日1回までです！また明日お試しください🌅", ephemeral=True)
        return

    # おみくじを引く
    fortune = secrets.choice(list(fortunes.keys()))
    flavor = fortunes[fortune]

    # データベースに記録
    c.execute("INSERT INTO draws (user_id, draw_date, fortune) VALUES (?, ?, ?)",
              (user_id, today, fortune))
    c.execute("UPDATE stats SET count = count + 1 WHERE fortune = ?", (fortune,))
    conn.commit()

    # 結果を送信
    await interaction.response.send_message(
        f"🎴 {interaction.user.mention} の運勢は **{fortune}**！\n{flavor}"
    )

# --- /omikuji_stats コマンド（出現回数確認） ---


@bot.tree.command(name="omikuji_stats", description="これまでの運勢の出現回数を表示します")
async def omikuji_stats(interaction: discord.Interaction):
    c.execute("SELECT fortune, count FROM stats ORDER BY count DESC")
    stats = c.fetchall()

    msg = "📊 **おみくじ運勢の出現回数**\n"
    for fortune, count in stats:
        msg += f"- **{fortune}**: {count}回\n"

    await interaction.response.send_message(msg)

# --- Botの起動 ---
# あなたのDiscord Botトークンに置き換えてください
bot.run(os.getenv("DISCORD_TOKEN"))
