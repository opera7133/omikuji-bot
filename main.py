import os
import sys
import logging
import secrets
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

import discord
from discord import app_commands
from discord.ext import commands
from google import genai
from google.genai import types

load_dotenv()

# --- ロギング設定 ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("omikuji_bot")

# --- 設定値と環境変数 ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
# デフォルトモデル: 高い文章生成能力と低コストを兼ね備えた最新のFlashモデル
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

if not DISCORD_TOKEN:
    logger.warning("DISCORD_TOKEN が設定されていません。.env ファイルを確認してください。")

gemini_client: genai.Client | None = None
if GOOGLE_API_KEY:
    gemini_client = genai.Client(api_key=GOOGLE_API_KEY)
else:
    logger.warning("GOOGLE_API_KEY が設定されていません。おみくじのフレーバーテキストは固定文が使用されます。")

# --- おみくじの種類とデフォルトテキスト ---
fortunes: dict[str, str] = {
    "大吉": "最高の一日になるでしょう！すべてがうまくいく予感。",
    "中吉": "良いことがありそう。前向きに行動してみて！",
    "小吉": "小さな幸せが訪れるかも。些細なことに感謝を。",
    "吉": "安定した一日。落ち着いて行動すると◎",
    "末吉": "控えめな行動が吉。焦らずじっくりと。",
    "凶": "今日は慎重に。無理せず休むのも大事です。"
}

# --- データベース管理 ---
DB_PATH = "omikuji.db"


def get_db() -> sqlite3.Connection:
    """SQLite接続を取得するコンテキストマネージャ対応ヘルパー"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """テーブルの初期化と初期データの投入"""
    with get_db() as conn:
        cursor = conn.cursor()
        # 抽選履歴テーブル
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS draws (
                user_id INTEGER,
                draw_date TEXT,
                fortune TEXT
            )
        ''')
        # 運勢の統計テーブル
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stats (
                fortune TEXT PRIMARY KEY,
                count INTEGER
            )
        ''')
        # 初期データを挿入（存在しない運勢のみ）
        for fortune in fortunes:
            cursor.execute(
                "INSERT OR IGNORE INTO stats (fortune, count) VALUES (?, ?)", (fortune, 0)
            )
        conn.commit()


# 初期化実行
init_db()


# --- フレーバーテキスト生成関数 ---
async def generate_flavor_text(fortune: str) -> str:
    """Gemini API を使って非同期でフレーバーテキストを生成する"""
    default_flavor = fortunes.get(fortune, "今日という一日を大切に過ごしましょう。")
    if not gemini_client:
        return default_flavor

    prompt = (
        f"「{fortune}」という運勢のフレーバーテキストを日本語で一行（20〜50文字程度）生成してください。\n"
        f"例: 「{default_flavor}」\n"
        "注意事項:\n"
        "- 運勢の名称（「大吉」など）は文頭や本文に含めず、フレーバーテキスト本文のみを出力してください。\n"
        "- 引用符（「」、\"\"など）は出力に含めないでください。\n"
        "- 出力する文章は毎回変えて、ユニークで味のある表現にしてください。"
    )

    try:
        # discord.py のイベントループをブロックしないよう非同期API (.aio) を使用
        response = await gemini_client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=1.0,
                max_output_tokens=100,
            )
        )
        text = response.text.strip() if response.text else ""
        # 前後の引用符や空白をクリーンアップ
        cleaned = text.strip('"\'「」\n\r ')
        return cleaned if cleaned else default_flavor
    except Exception as e:
        logger.warning("Gemini API呼び出しに失敗しました (モデル: %s): %s。デフォルトテキストを使用します。", GEMINI_MODEL, e)
        return default_flavor


# --- Bot クラスの定義 ---
class OmikujiBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # 起動時に1回だけスラッシュコマンドを同期（on_readyでの多重呼び出し・レート制限を防止）
        await self.tree.sync()
        logger.info("スラッシュコマンドの同期が完了しました。")


bot = OmikujiBot()


class Confirm(discord.ui.View):
    """データベースリセット確認用 UI View"""
    def __init__(self, timeout: float = 60.0):
        super().__init__(timeout=timeout)
        self.value: bool | None = None

    @discord.ui.button(label='リセット実行', style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        self.stop()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="⏳ データベースをリセットしています...", view=self)

    @discord.ui.button(label='キャンセル', style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        self.stop()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="❌ データベースのリセットをキャンセルしました。", view=self)


def is_admin(interaction: discord.Interaction) -> bool:
    return bool(interaction.user.guild_permissions.administrator)


@bot.event
async def on_ready():
    logger.info("Logged in as %s (ID: %s)", bot.user.name, bot.user.id)


# --- /omikuji コマンド ---
@bot.tree.command(name="omikuji", description="今日の運勢を占います（1日1回）")
async def omikuji(interaction: discord.Interaction):
    await interaction.response.defer()
    user_id = interaction.user.id
    today = datetime.now(ZoneInfo("Asia/Tokyo")).date().isoformat()

    # 既に今日引いたか確認
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM draws WHERE user_id = ? AND draw_date = ?", (user_id, today)
        )
        if cursor.fetchone():
            await interaction.followup.send("おみくじは1日1回までです！また明日お試しください🌅", ephemeral=True)
            return

    # おみくじを引く
    fortune = secrets.choice(list(fortunes.keys()))
    flavor = await generate_flavor_text(fortune)

    # データベースに記録
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO draws (user_id, draw_date, fortune) VALUES (?, ?, ?)",
            (user_id, today, fortune)
        )
        cursor.execute(
            "UPDATE stats SET count = count + 1 WHERE fortune = ?",
            (fortune,)
        )
        conn.commit()

    # 結果を送信
    await interaction.followup.send(
        content=f"🎴 {interaction.user.mention} の運勢は **{fortune}**！\n{flavor}"
    )


# --- /omikuji_stats コマンド（出現回数確認） ---
@bot.tree.command(name="omikuji_stats", description="これまでの運勢の出現回数を表示します")
async def omikuji_stats(interaction: discord.Interaction):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT fortune, count FROM stats ORDER BY count DESC")
        stats = cursor.fetchall()

    msg = "📊 **おみくじ運勢の出現回数**\n"
    for row in stats:
        msg += f"- **{row['fortune']}**: {row['count']}回\n"

    await interaction.response.send_message(msg)


# --- /omikuji_history コマンド（履歴表示） ---
@bot.tree.command(name="omikuji_history", description="直近1週間のおみくじ履歴を表示します")
async def omikuji_history(interaction: discord.Interaction):
    user_id = interaction.user.id
    today = datetime.now(ZoneInfo("Asia/Tokyo")).date()
    week_ago = today - timedelta(days=6)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT draw_date, fortune FROM draws WHERE user_id = ? AND draw_date BETWEEN ? AND ? ORDER BY draw_date DESC",
            (user_id, week_ago.isoformat(), today.isoformat())
        )
        rows = cursor.fetchall()

    if not rows:
        await interaction.response.send_message("直近1週間のおみくじ履歴はありません。", ephemeral=True)
        return

    msg = f"📅 **{interaction.user.display_name}さんの直近1週間のおみくじ履歴**\n"
    for row in rows:
        msg += f"- {row['draw_date']}: **{row['fortune']}**\n"

    await interaction.response.send_message(msg)


# --- /omikuji_db_reset コマンド（管理者専用：DB完全リセット） ---
@bot.tree.command(name="omikuji_db_reset", description="【管理者専用】データベースを完全リセットします（要確認）")
@app_commands.check(is_admin)
async def omikuji_db_reset(interaction: discord.Interaction):
    view = Confirm()
    await interaction.response.send_message(
        "⚠️ **本当にデータベースを完全リセットしますか？**\nボタンを押すと全ユーザーのおみくじ履歴と統計データが消去されます。",
        view=view,
        ephemeral=True
    )
    timed_out = await view.wait()
    if timed_out or view.value is None:
        await interaction.edit_original_response(
            content="⏱️ 確認がタイムアウトしました。リセットは行われませんでした。",
            view=None
        )
        return

    if view.value:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("DROP TABLE IF EXISTS draws")
            cursor.execute("DROP TABLE IF EXISTS stats")
            conn.commit()
        # テーブルを再作成して初期化
        init_db()
        await interaction.edit_original_response(
            content="✅ データベースを完全リセットしました。",
            view=None
        )


# --- /omikuji_today_reset コマンド（管理者専用：今日の運勢リセット全ユーザ） ---
@bot.tree.command(name="omikuji_today_reset", description="【管理者専用】今日の運勢記録を全ユーザ分リセットします")
@app_commands.check(is_admin)
async def omikuji_today_reset(interaction: discord.Interaction):
    today = datetime.now(ZoneInfo("Asia/Tokyo")).date().isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM draws WHERE draw_date = ?", (today,))
        conn.commit()
    await interaction.response.send_message("✅ 今日の運勢記録を全ユーザ分リセットしました。", ephemeral=True)


# --- /omikuji_user_today_reset コマンド（管理者専用：指定ユーザの今日のリセット） ---
@bot.tree.command(name="omikuji_user_today_reset", description="【管理者専用】指定ユーザの今日の運勢記録をリセットします")
@app_commands.describe(user="リセットしたいユーザ")
@app_commands.check(is_admin)
async def omikuji_user_today_reset(interaction: discord.Interaction, user: discord.User):
    today = datetime.now(ZoneInfo("Asia/Tokyo")).date().isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM draws WHERE user_id = ? AND draw_date = ?", (user.id, today))
        conn.commit()
    await interaction.response.send_message(f"✅ {user.mention} の今日の運勢記録をリセットしました。", ephemeral=True)


# --- /lottery コマンド（チャンネル内ランダム抽選） ---
@bot.tree.command(name="lottery", description="このチャンネル内の誰かをランダムに抽選します")
async def lottery(interaction: discord.Interaction):
    await interaction.response.defer()

    if not interaction.guild:
        await interaction.followup.send("このコマンドはサーバー内のチャンネルでのみ使用できます。", ephemeral=True)
        return

    # チャンネル内のメンバーを取得（ボットおよびコマンド実行者を除外）
    members = [
        member for member in interaction.channel.members
        if not member.bot and member.id != interaction.user.id
    ]

    if not members:
        await interaction.followup.send("抽選対象者がいません（他の参加メンバーが必要です）。", ephemeral=True)
        return

    selected = secrets.choice(members)
    await interaction.followup.send(f"🎉 **{selected.mention}** さんが選ばれました！")


# --- Botの起動 ---
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN が設定されていないため、Botを起動できません。")
        sys.exit(1)

    bot.run(DISCORD_TOKEN)
