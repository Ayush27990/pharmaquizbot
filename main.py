import os
import json
import re
import time
import random
import logging
import fitz  # PyMuPDF

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters
)

from groq import Groq

# ======================
# CONFIG
# ======================
logging.basicConfig(level=logging.INFO)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
CHANNEL_ID = os.getenv("CHANNEL_ID")

ADMIN_ID = 723919716

client = Groq(api_key=GROQ_API_KEY)

pending_questions = {}

# ======================
# HIGH-YIELD TOPICS
# ======================
TOPICS = [
    "G6PD deficiency hemolysis oxidative stress",
    "Pyruvate kinase deficiency anemia",
    "Acute intermittent porphyria heme synthesis",
    "Maple syrup urine disease",
    "Ornithine transcarbamylase deficiency",
    "Warfarin vitamin K cycle",
    "Organophosphate poisoning",
    "Phenytoin toxicity",
    "Methotrexate toxicity",
    "Tumor lysis syndrome",
]

# ======================
# SAFE JSON PARSER (FIX CRASHES)
# ======================
def extract_json(text):
    try:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start == -1 or end == -1:
            return None
        return json.loads(text[start:end])
    except Exception as e:
        logging.error(f"JSON parse error: {e}")
        return None

# ======================
# GENERATE MCQ
# ======================
async def generate_mcq(content):
    prompt = f"""
You are a NEET PG examiner.

Generate ONE HIGH-YIELD MCQ.

Content: {content}

Return ONLY JSON:
{{
 "question": "...",
 "options": ["A) ...", "B) ...", "C) ...", "D) ..."],
 "answer_index": 0,
 "explanation": "..."
}}
"""

    response = client.chat.completions.create(
        model="llama3-70b-8192",
        messages=[{"role": "user", "content": prompt}]
    )

    return extract_json(response.choices[0].message.content)

# ======================
# REVIEW MCQ
# ======================
async def review_mcq(mcq):
    prompt = f"""
Rate MCQ 1–10:

Q: {mcq['question']}
Options: {mcq['options']}
Explanation: {mcq['explanation']}

Return JSON:
{{
 "score": 0
}}
"""

    response = client.chat.completions.create(
        model="llama3-70b-8192",
        messages=[{"role": "user", "content": prompt}]
    )

    return extract_json(response.choices[0].message.content)

# ======================
# PDF TEXT EXTRACTION
# ======================
def extract_pdf_text(path):
    doc = fitz.open(path)
    text = ""
    for page in doc:
        text += page.get_text()
    return text[:10000]

# ======================
# ADMIN APPROVAL
# ======================
async def send_for_approval(context, mcq, source):
    qid = str(int(time.time()))
    pending_questions[qid] = mcq

    text = (
        f"📋 MCQ FOR APPROVAL\n\n"
        f"📚 {source}\n\n"
        f"{mcq['question']}\n\n"
        + "\n".join(mcq["options"]) +
        f"\n\n💡 {mcq['explanation']}"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_{qid}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject_{qid}")
        ]
    ])

    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=text,
        reply_markup=keyboard
    )

# ======================
# AUTO MCQ (15 MIN SAFE)
# ======================
async def scheduled_job(context: ContextTypes.DEFAULT_TYPE):
    topic = random.choice(TOPICS)

    mcq = await generate_mcq(topic)
    if not mcq:
        return

    review = await review_mcq(mcq)
    if not review:
        return

    if review.get("score", 0) >= 8:
        await send_for_approval(context, mcq, f"AUTO: {topic}")

# ======================
# PDF HANDLER
# ======================
async def handle_pdf(update, context):
    if update.effective_user.id != ADMIN_ID:
        return

    file = await update.message.document.get_file()
    path = f"/tmp/{update.message.document.file_name}"
    await file.download_to_drive(path)

    text = extract_pdf_text(path)

    await update.message.reply_text("📄 PDF processed... generating MCQ")

    mcq = await generate_mcq(text)

    if mcq:
        review = await review_mcq(mcq)

        if review and review.get("score", 0) >= 8:
            await send_for_approval(context, mcq, "PDF")
        else:
            await update.message.reply_text("⚠️ Low quality MCQ rejected")

# ======================
# CALLBACK
# ======================
async def handle_callback(update, context):
    query = update.callback_query
    await query.answer()

    data = query.data

    if data.startswith("approve_"):
        qid = data.split("_")[1]
        mcq = pending_questions.get(qid)

        if mcq:
            text = (
                f"📚 APPROVED MCQ\n\n"
                f"{mcq['question']}\n\n"
                + "\n".join(mcq["options"]) +
                f"\n\n💡 Explanation:\n||{mcq['explanation']}||"
            )

            await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=text
            )

        pending_questions.pop(qid, None)
        await query.edit_message_text("✅ Posted")

    elif data.startswith("reject_"):
        qid = data.split("_")[1]
        pending_questions.pop(qid, None)
        await query.edit_message_text("❌ Rejected")

# ======================
# START
# ======================
async def start(update, context):
    await update.message.reply_text(
        "🚀 Bot Running\n"
        "✔ Auto MCQs (15 min)\n"
        "✔ PDF MCQs\n"
        "✔ AI review\n"
        "✔ Admin approval"
    )

# ======================
# MAIN
# ======================
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))

    # FIXED JOB QUEUE (stable)
    app.job_queue.run_repeating(
        scheduled_job,
        interval=900,
        first=10
    )

    print("Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
