from google import genai
import discord
from discord.ext import commands
from discord import app_commands
import secrets
import sqlite3
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
load_dotenv()


GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
gemini = genai.Client(api_key=GOOGLE_API_KEY)

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


class Confirm(discord.ui.View):
    def __init__(self):
        super().__init__()
        self.value = None

    @discord.ui.button(label='確認', style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message('確認中', ephemeral=True)
        self.value = True
        self.stop()

    @discord.ui.button(label='キャンセル', style=discord.ButtonStyle.grey)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message('キャンセル中', ephemeral=True)
        self.value = False
        self.stop()


def is_admin(interaction: discord.Interaction):
    return interaction.user.guild_permissions.administrator


@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f'Logged in as {bot.user.name}')


# --- /omikuji コマンド ---
@bot.tree.command(name="omikuji", description="今日の運勢を占います（1日1回）")
async def omikuji(interaction: discord.Interaction):
    await interaction.response.defer()
    user_id = interaction.user.id
    today = datetime.now(ZoneInfo("Asia/Tokyo")).date().isoformat()

    # 既に今日引いたか確認
    c.execute(
        "SELECT * FROM draws WHERE user_id = ? AND draw_date = ?", (user_id, today))
    if c.fetchone():
        await interaction.followup.send("おみくじは1日1回までです！また明日お試しください🌅", ephemeral=True)
        return

    # おみくじを引く
    fortune = secrets.choice(list(fortunes.keys()))
    flavor = fortunes[fortune]
    try:
        flavor_res = gemini.models.generate_content(
            model='gemini-2.0-flash',
            contents=f"「{fortune}」という運勢のフレーバーテキストを日本語で一行だけ生成してください。出力は「{fortunes[fortune]}」のように、文章だけにしてください。出力する文章は変えてください。")
        flavor = flavor_res.text
    except:
        print("Gemini APIの呼び出しに失敗しました。デフォルトのフレーバーテキストを使用します。")

    # データベースに記録
    c.execute("INSERT INTO draws (user_id, draw_date, fortune) VALUES (?, ?, ?)",
              (user_id, today, fortune))
    c.execute("UPDATE stats SET count = count + 1 WHERE fortune = ?", (fortune,))
    conn.commit()

    # 結果を送信
    await interaction.followup.send(
        content=f"🎴 {interaction.user.mention} の運勢は **{fortune}**！\n{flavor}"
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

# ...existing code...


@bot.tree.command(name="omikuji_history", description="直近1週間のおみくじ履歴を表示します（自分のみ）")
async def omikuji_history(interaction: discord.Interaction):
    user_id = interaction.user.id
    today = datetime.now(ZoneInfo("Asia/Tokyo")).date()
    week_ago = today - timedelta(days=6)
    c.execute(
        "SELECT draw_date, fortune FROM draws WHERE user_id = ? AND draw_date BETWEEN ? AND ? ORDER BY draw_date DESC",
        (user_id, week_ago.isoformat(), today.isoformat())
    )
    rows = c.fetchall()
    if not rows:
        await interaction.response.send_message("直近1週間のおみくじ履歴はありません。", ephemeral=True)
        return

    msg = f"📅 **{interaction.user.display_name}さんの直近1週間のおみくじ履歴**\n"
    for draw_date, fortune in rows:
        msg += f"- {draw_date}: **{fortune}**\n"
    await interaction.response.send_message(msg)

# ...existing code...
# データベース完全リセット


@bot.tree.command(name="omikuji_db_reset", description="【管理者専用】データベースを完全リセットします（要確認）")
@app_commands.check(is_admin)
async def omikuji_db_reset(interaction: discord.Interaction):
    view = Confirm()
    await interaction.response.send_message(
        "⚠️ 本当にデータベースを完全リセットしますか？ `/omikuji_db_reset_confirm` を実行すると全データが消えます。",
        view=view,
        ephemeral=True
    )
    await view.wait()
    await interaction.delete_original_response()
    if view.value is None:
        return
    elif view.value:
        c.execute("DROP TABLE IF EXISTS draws")
        c.execute("DROP TABLE IF EXISTS stats")
        # 再作成
        c.execute('''
            CREATE TABLE IF NOT EXISTS draws (
                user_id INTEGER,
                draw_date TEXT,
                fortune TEXT
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS stats (
                fortune TEXT PRIMARY KEY,
                count INTEGER
            )
        ''')
        for fortune in fortunes:
            c.execute(
                "INSERT OR IGNORE INTO stats (fortune, count) VALUES (?, ?)", (fortune, 0))
        conn.commit()
        await interaction.followup.send("✅ データベースを完全リセットしました。", ephemeral=True)
        return
    else:
        await interaction.followup.send("❌ データベースのリセットをキャンセルしました。", ephemeral=True)
        return

# 今日の運勢のリセット（全ユーザ）


@bot.tree.command(name="omikuji_today_reset", description="【管理者専用】今日の運勢記録を全ユーザ分リセットします")
@app_commands.check(is_admin)
async def omikuji_today_reset(interaction: discord.Interaction):
    today = datetime.now(ZoneInfo("Asia/Tokyo")).date().isoformat()
    c.execute("DELETE FROM draws WHERE draw_date = ?", (today,))
    conn.commit()
    await interaction.response.send_message("✅ 今日の運勢記録を全ユーザ分リセットしました。", ephemeral=True)

# 特定ユーザの今日の運勢リセット


@bot.tree.command(name="omikuji_user_today_reset", description="【管理者専用】指定ユーザの今日の運勢記録をリセットします")
@app_commands.describe(user="リセットしたいユーザ")
@app_commands.check(is_admin)
async def omikuji_user_today_reset(interaction: discord.Interaction, user: discord.User):
    today = datetime.now(ZoneInfo("Asia/Tokyo")).date().isoformat()
    c.execute("DELETE FROM draws WHERE user_id = ? AND draw_date = ?",
              (user.id, today))
    conn.commit()
    await interaction.response.send_message(f"✅ {user.mention} の今日の運勢記録をリセットしました。", ephemeral=True)


# --- Botの起動 ---
# あなたのDiscord Botトークンに置き換えてください
bot.run(os.getenv("DISCORD_TOKEN"))
