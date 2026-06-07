import os
import json
import re
import logging
import asyncio
from datetime import datetime
from telegram import Bot
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from groq import Groq

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
CHANNEL_ID = os.getenv("CHANNEL_ID")

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN is missing")
if not GROQ_API_KEY:
    raise ValueError("GROQ_API_KEY is missing")
if not CHANNEL_ID:
    raise ValueError("CHANNEL_ID is missing")

groq_client = Groq(api_key=GROQ_API_KEY)

TOPICS = [
    "Biochemistry enzymes and cofactors",
    "Pharmacology beta blockers",
    "Biochemistry amino acid metabolism",
    "Pharmacology antibiotics mechanism",
    "Biochemistry lipid metabolism",
    "Pharmacology antihypertensive drugs",
    "Biochemistry carbohydrate metabolism",
    "Pharmacology antifungal drugs",
    "Biochemistry DNA replication",
    "Pharmacology diuretics",
    "Biochemistry vitamins and deficiencies",
    "Pharmacology antidiabetic drugs",
    "Biochemistry urea cycle",
    "Pharmacology anticoagulants",
    "Biochemistry electron transport chain",
    "Pharmacology antiepileptic drugs",
    "Biochemistry protein synthesis",
    "Pharmacology antipsychotic drugs",
    "Biochemistry glycolysis",
    "Pharmacology NSAIDs mechanism",
]

topic_index = 0

def escape_md(text):
    for ch in ["_", "*", "[", "]", "(", ")", "~", "`", ">", "#", "+", "-", "=", "|", "{", "}", ".", "!"]:
        text = text.replace(ch, f"\\{ch}")
    return text

def extract_json(text):
    try:
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if not match:
            return []
        return json.loads(match.group())
    except Exception as e:
        logger.error(f"JSON extraction error: {e}")
        return []

async def generate_question(topic):
    prompt = f"""You are an expert medical educator.
Generate exactly 1 high-quality multiple-choice question about: {topic}

Rules:
- Four options
- One correct answer
- Clinical or applied style
- Detailed explanation
- Explain why each wrong option is incorrect

Return ONLY JSON in this format:
[{{
  "question": "...",
  "options": ["A) ...", "B) ...", "C) ...", "D) ..."],
  "answer_index": 0,
  "explanation": "Correct: A because... B is wrong because... C is wrong because... D is wrong because..."
}}]"""

    response = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}]
    )
    raw = response.choices[0].message.content
    questions = extract_json(raw)
    return questions[0] if questions else None

async def send_scheduled_quiz(context):
    global topic_index
    try:
        topic = TOPICS[topic_index % len(TOPICS)]
        topic_index += 1
        logger.info(f"Generating question on: {topic}")
        question = await generate_question(topic)
        if not question:
            logger.error("Failed to generate question")
            return
        text_msg = (
            f"📚 *{topic.upper()}*\n\n"
            f"*{question['question']}*\n\n"
            + "\n".join(question["options"])
        )
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=text_msg,
            parse_mode="Markdown"
        )
        await asyncio.sleep(2)
        await context.bot.send_poll(
            chat_id=CHANNEL_ID,
            question=question["question"][:300],
            options=[opt[3:] if opt[1] == ")" else opt for opt in question["options"]],
            type="quiz",
            correct_option_id=int(question["answer_index"]),
            is_anonymous=True,
            explanation=question["explanation"][:200] if len(question["explanation"]) <= 200 else None
        )
        await asyncio.sleep(2)
        explanation = question["explanation"]
        result_escaped = escape_md("💡 Explanation:")
        explanation_escaped = escape_md(explanation)
        spoiler_text = f"{result_escaped}\n\n||{explanation_escaped}||"
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=spoiler_text,
            parse_mode="MarkdownV2"
        )
        logger.info(f"Successfully sent quiz on: {topic}")
    except Exception as e:
        logger.error(f"Error sending quiz: {e}")

async def start(update, context):
    await update.message.reply_text(
        "✅ Pharma Quiz Bot is running!\n"
        "Sending Biochemistry & Pharmacology MCQs every 15 minutes to the channel."
    )

async def post_now(update, context):
    await send_scheduled_quiz(context)
    await update.message.reply_text("✅ Question posted!")

def main():
    logger.info("Starting Pharma Quiz Scheduler Bot...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("postnow", post_now))
    app.job_queue.run_repeating(
        send_scheduled_quiz,
        interval=900,
        first=10
    )
    logger.info("Scheduler started - posting every 15 minutes")
    app.run_polling()

if __name__ == "__main__":
    main()
