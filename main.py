import os
import json
import re
import logging
import asyncio
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
from groq import Groq

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMIN_ID = 723919716

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
pending_questions = {}

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
Return ONLY JSON:
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

async def send_for_approval(context, question, topic):
    global pending_questions
    preview = (
        f"📋 *NEW QUESTION FOR APPROVAL*\n\n"
        f"📚 Topic: {topic}\n\n"
        f"*{question['question']}*\n\n"
        + "\n".join(question["options"]) +
        f"\n\n✅ Correct: {question['options'][question['answer_index']]}\n\n"
        f"💡 Explanation: {question['explanation']}"
    )
    import time
    question_id = str(int(time.time()))
    pending_questions[question_id] = {
        "question": question,
        "topic": topic
    }
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve & Post", callback_data=f"approve_{question_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject_{question_id}")
        ]
    ])
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=preview,
        parse_mode="Markdown",
        reply_markup=keyboard
    )

async def post_to_channel(context, question, topic):
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
    clean_options = []
    for opt in question["options"]:
        if len(opt) > 2 and opt[1] == ")":
            clean_options.append(opt[3:].strip())
        else:
            clean_options.append(opt)
    await context.bot.send_poll(
        chat_id=CHANNEL_ID,
        question=question["question"][:300],
        options=clean_options,
        type="quiz",
        correct_option_id=int(question["answer_index"]),
        is_anonymous=True
    )
    await asyncio.sleep(2)
    explanation_escaped = escape_md(question["explanation"])
    spoiler_text = f"💡 Explanation:\n\n||{explanation_escaped}||"
    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=spoiler_text,
        parse_mode="MarkdownV2"
    )

async def handle_callback(update, context):
    global topic_index
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("approve_"):
        question_id = data.replace("approve_", "")
        if question_id in pending_questions:
            item = pending_questions.pop(question_id)
            await post_to_channel(context, item["question"], item["topic"])
            await query.edit_message_text("✅ Question approved and posted to channel!")
    elif data.startswith("reject_"):
        question_id = data.replace("reject_", "")
        if question_id in pending_questions:
            pending_questions.pop(question_id)
            await query.edit_message_text("❌ Question rejected. Next one will come in 15 minutes.")

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
        await send_for_approval(context, question, topic)
        logger.info(f"Question sent for approval: {topic}")
    except Exception as e:
        logger.error(f"Error: {e}")

async def start(update, context):
    if update.effective_user.id == ADMIN_ID:
        await update.message.reply_text(
            "✅ Pharma Quiz Bot is running!\n"
            "Questions will be sent to you every 15 minutes for approval.\n\n"
            "Commands:\n"
            "/postnow - Generate a question now"
        )

async def post_now(update, context):
    if update.effective_user.id == ADMIN_ID:
        await update.message.reply_text("⏳ Generating question...")
        await send_scheduled_quiz(context)

def main():
    logger.info("Starting Pharma Quiz Bot...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("postnow", post_now))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.job_queue.run_repeating(
        send_scheduled_quiz,
        interval=900,
        first=10
    )
    logger.info("Bot started!")
    app.run_polling()

if __name__ == "__main__":
    main()
