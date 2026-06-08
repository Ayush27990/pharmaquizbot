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
    "Maple syrup urine disease amino acid metabolism",
    "Ornithine transcarbamylase deficiency urea cycle",
    "Warfarin vitamin K cycle mechanism",
    "Organophosphate poisoning acetylcholinesterase inhibition",
    "Phenytoin toxicity cerebellar signs",
    "Methotrexate toxicity folate metabolism",
    "Tumor lysis syndrome hyperuricemia",
]

# ======================
# JSON PARSER
# ======================
def extract_json(text):
    try:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        return json.loads(match.group())
    except:
        return None

# ======================
# GROQ MCQ GENERATION
# ======================
async def generate_mcq(content, mode="auto"):
    prompt = f"""
You are a NEET PG / INICET examiner.

Generate ONE HIGH-YIELD MCQ.

Mode: {mode}

Content:
{content}

Rules:
- Clinical vignette style
- 4 options (A–D)
- Only one correct answer
- High difficulty
- Explain all options

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
# REVIEW SYSTEM
# ======================
async def review_mcq(mcq):
    prompt = f"""
You are a NEET PG question reviewer.

Score this MCQ 1–10:

Q: {mcq['question']}
Options: {mcq['options']}
Explanation: {mcq['explanation']}

Check:
- Clinical relevance
- Difficulty
- Distractors quality

Return ONLY JSON:
{{
 "score": 0,
 "approved": false
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
    return text[:12000]

# ======================
# SEND TO ADMIN
# ======================
async def send_for_approval(context, mcq, source):
    qid = str(int(time.time()))
    pending_questions[qid] = mcq

    text = (
        f"📋 MCQ FOR APPROVAL\n\n"
        f"📚 Source: {source}\n\n"
        f"{mcq['question']}\n\n"
        + "\n".join(mcq["options"]) +
        f"\n\n💡 Explanation:\n{mcq['explanation']}"
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
# AUTO MCQ (15 MIN)
# ======================
async def scheduled_job(context: ContextTypes.DEFAULT_TYPE):
    topic = random.choice(TOPICS)

    attempts = 0
    mcq = None

    while attempts < 2:
        attempts += 1

        mcq = await generate_mcq(topic, mode="auto")
        if not mcq:
            continue

        review = await review_mcq(mcq)
        if review and review.get("score", 0) >= 8.5:
            break
        else:
            mcq = None

    if mcq:
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

    await update.message.reply_text("📄 PDF received. Generating MCQ...")

    mcq = await generate_mcq(text, mode="PDF")

    if mcq:
        review = await review_mcq(mcq)

        if review and review.get("score", 0) >= 8:
            await send_for_approval(context, mcq, "PDF")
        else:
            await update.message.reply_text("⚠️ Low quality MCQ rejected")

# ======================
# CALLBACK HANDLER
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
        "🚀 Bot Running\n\n"
        "✔ Auto MCQs (15 min)\n"
        "✔ PDF MCQ generation\n"
        "✔ AI review system\n"
        "✔ Admin approval system"
    )

# ======================
# MAIN
# ======================
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))

    # PDF handler
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))

    # AUTO MCQ EVERY 15 MIN
    app.job_queue.run_repeating(
        scheduled_job,
        interval=900,
        first=10
    )

    print("Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
