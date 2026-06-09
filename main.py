import os
import json
import re
import time
import logging
import asyncio
import io
import base64
import random

import PyPDF2
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
# LOGGING
# ======================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ======================
# CONFIG
# ======================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMIN_ID = 723919716
INTERVAL = 3600  # 1 hour

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN missing")
if not GROQ_API_KEY:
    raise ValueError("GROQ_API_KEY missing")
if not CHANNEL_ID:
    raise ValueError("CHANNEL_ID missing")

client = Groq(api_key=GROQ_API_KEY)
pending_questions = {}
used_topics = []

# ======================
# BOOK TOPIC POOLS
# ======================
HARPER_TOPICS = [
    "Urea cycle disorders and hyperammonemia",
    "Glycolysis regulation and Pasteur effect",
    "HMP shunt and NADPH production",
    "Fatty acid synthesis vs beta-oxidation",
    "Cholesterol synthesis and HMG-CoA reductase",
    "Heme synthesis and porphyrias",
    "Protein folding and chaperones",
    "DNA replication and repair mechanisms",
    "RNA processing and splicing",
    "Enzyme kinetics Km and Vmax",
    "Citric acid cycle regulation",
    "Electron transport chain and oxidative phosphorylation",
    "Gluconeogenesis key enzymes",
    "Glycogen storage diseases",
    "Lipoprotein metabolism HDL LDL",
    "Amino acid catabolism and transamination",
    "Purine and pyrimidine synthesis",
    "Collagen synthesis and scurvy",
    "Hemoglobin structure and cooperativity",
    "Biotransformation and cytochrome P450",
    "Vitamins B1 B2 B3 coenzyme roles",
    "Vitamin B12 and folate metabolism",
    "Iron absorption and transport",
    "Signal transduction cAMP pathway",
    "Calcium signaling and calmodulin",
]

GOODMAN_TOPICS = [
    "Warfarin mechanism and vitamin K antagonism",
    "Beta blocker pharmacology and selectivity",
    "ACE inhibitors vs ARBs mechanisms",
    "Diuretics loop vs thiazide vs potassium sparing",
    "Antiepileptic drugs mechanisms",
    "Benzodiazepine vs barbiturate GABA mechanism",
    "Opioid receptors and analgesic ladder",
    "NSAIDs COX selectivity and side effects",
    "Aminoglycoside mechanism and toxicity",
    "Beta lactam antibiotics mechanism and resistance",
    "Antifungal drugs mechanisms azoles vs polyenes",
    "Antituberculosis drugs first line mechanisms",
    "Antidiabetic drugs insulin sensitizers vs secretagogues",
    "Thyroid hormone pharmacology",
    "Corticosteroid pharmacology and adverse effects",
    "Antipsychotic D2 receptor blockade",
    "Antidepressants SSRI vs TCA vs MAOI",
    "Anticholinergic drugs atropine uses",
    "Cholinergic drugs and organophosphate poisoning",
    "Cardiac glycosides digoxin mechanism",
    "Antiarrhythmic drugs Vaughan Williams classification",
    "Statins mechanism and myopathy",
    "Anticoagulants heparin vs LMWH vs DOACs",
    "Cancer chemotherapy alkylating agents",
    "Immunosuppressants cyclosporine tacrolimus",
]

# ======================
# HELPERS
# ======================
def escape_md(text):
    for ch in ["_", "*", "[", "]", "(", ")", "~", "`", ">",
               "#", "+", "-", "=", "|", "{", "}", ".", "!"]:
        text = text.replace(ch, f"\\{ch}")
    return text

def extract_json(text):
    try:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group())
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            result = json.loads(match.group())
            if isinstance(result, list) and len(result) > 0:
                return result[0]
        return None
    except Exception as e:
        logger.error(f"JSON parse error: {e}")
        return None

async def safe_groq_call(**kwargs):
    for attempt in range(3):
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as e:
            if "rate_limit" in str(e).lower() or "429" in str(e):
                wait = 30 * (attempt + 1)
                logger.warning(f"Rate limit hit, waiting {wait}s...")
                await asyncio.sleep(wait)
            else:
                logger.error(f"Groq error: {e}")
                return None
    return None

# ======================
# GENERATE TOPIC
# ======================
async def generate_topic(book=None):
    if book == "harper":
        return random.choice(HARPER_TOPICS)
    elif book == "goodman":
        return random.choice(GOODMAN_TOPICS)

    used = ", ".join(used_topics[-20:]) if used_topics else "none"
    prompt = (
        "You are a NEET PG / FMGE / USMLE medical expert.\n\n"
        "Suggest ONE specific high-yield topic for a biochemistry or pharmacology MCQ.\n\n"
        "Already used topics (avoid repeating): " + used + "\n\n"
        "Requirements:\n"
        "- Must be specific and clinically relevant\n"
        "- Alternate between biochemistry and pharmacology\n"
        "- Focus on NEET PG high yield topics\n\n"
        'Return ONLY JSON: {"topic": "Warfarin mechanism and vitamin K cycle"}'
    )
    try:
        response = await safe_groq_call(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.9
        )
        if not response:
            return random.choice(HARPER_TOPICS + GOODMAN_TOPICS)
        result = extract_json(response.choices[0].message.content)
        topic = result.get("topic") if result else random.choice(HARPER_TOPICS)
        used_topics.append(topic)
        if len(used_topics) > 100:
            used_topics.pop(0)
        return topic
    except Exception as e:
        logger.error("Topic generation error: " + str(e))
        return random.choice(HARPER_TOPICS + GOODMAN_TOPICS)

# ======================
# GENERATE MCQ
# ======================
async def generate_mcq(content, book_context=None):
    if book_context == "harper":
        source_context = (
            "Based on Harper's Illustrated Biochemistry 33rd Edition. "
            "Reference Harper's chapter topics, enzyme names, and clinical correlations."
        )
    elif book_context == "goodman":
        source_context = (
            "Based on Goodman & Gilman's Pharmacological Basis of Therapeutics. "
            "Reference drug mechanisms, receptor pharmacology, and clinical applications."
        )
    else:
        source_context = "Based on standard NEET PG / USMLE medical curriculum."

    prompt = (
        "You are a NEET PG / USMLE / FMGE expert examiner.\n\n"
        + source_context + "\n\n"
        "Generate ONE high-yield clinical MCQ based on: " + content + "\n\n"
        "Rules:\n"
        "- Clinical vignette style with patient scenario\n"
        "- 4 options, one definitively correct\n"
        "- No ambiguous or trick questions\n"
        "- Explanation must cite mechanism clearly\n"
        "- Explain why each wrong option is incorrect\n\n"
        "Return ONLY this JSON:\n"
        '{"question": "A patient presents with...", '
        '"options": ["A) ...", "B) ...", "C) ...", "D) ..."], '
        '"answer_index": 0, '
        '"explanation": "Correct: A because... B is wrong because..."}'
    )
    try:
        response = await safe_groq_call(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3
        )
        if not response:
            return None
        return extract_json(response.choices[0].message.content)
    except Exception as e:
        logger.error("MCQ generation error: " + str(e))
        return None

# ======================
# GENERATE MCQ FROM IMAGE
# ======================
async def generate_mcq_from_image(image_bytes, mime_type="image/jpeg"):
    try:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        vision_response = await safe_groq_call(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime_type};base64,{b64}"}
                        },
                        {
                            "type": "text",
                            "text": "Extract all medical/biochemistry/pharmacology text from this image. Return raw text only."
                        }
                    ]
                }
            ],
            temperature=0.1
        )
        if not vision_response:
            return None, "Vision model unavailable"
        extracted_text = vision_response.choices[0].message.content
        if not extracted_text or len(extracted_text.strip()) < 20:
            return None, "Could not extract text from image"
        extracted_text = extracted_text[:4000]
        await asyncio.sleep(20)
        mcq = await generate_mcq(extracted_text)
        return mcq, extracted_text[:200]
    except Exception as e:
        logger.error("Image MCQ error: " + str(e))
        return None, str(e)

# ======================
# SEND FOR APPROVAL
# ======================
async def send_for_approval(bot, mcq, source):
    try:
        qid = str(int(time.time()))
        pending_questions[qid] = {"mcq": mcq, "source": source}
        correct_option = mcq["options"][mcq["answer_index"]]
        text = (
            "📋 NEW MCQ FOR APPROVAL\n\n"
            "📚 Source: " + source + "\n\n"
            + mcq["question"] + "\n\n"
            + "\n".join(mcq["options"])
            + "\n\n✅ Correct: " + correct_option
            + "\n\n💡 Explanation:\n" + mcq["explanation"]
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve & Post", callback_data="approve_" + qid),
                InlineKeyboardButton("❌ Reject", callback_data="reject_" + qid)
            ],
            [
                InlineKeyboardButton("🔄 Regenerate", callback_data="regen_" + qid)
            ]
        ])
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=text,
            reply_markup=keyboard
        )
        logger.info("MCQ sent for approval: " + source)
    except Exception as e:
        logger.error("Send for approval error: " + str(e))

# ======================
# POST TO CHANNEL
# ======================
async def post_to_channel(bot, mcq):
    try:
        text_msg = (
            "📚 DAILY MCQ\n\n"
            + mcq["question"] + "\n\n"
            + "\n".join(mcq["options"])
        )
        await bot.send_message(chat_id=CHANNEL_ID, text=text_msg)
        await asyncio.sleep(2)

        clean_options = []
        for opt in mcq["options"]:
            if len(opt) > 2 and opt[1] == ")":
                clean_options.append(opt[3:].strip())
            else:
                clean_options.append(opt)

        await bot.send_poll(
            chat_id=CHANNEL_ID,
            question=mcq["question"][:300],
            options=clean_options,
            type="quiz",
            correct_option_id=int(mcq["answer_index"]),
            is_anonymous=True
        )
        await asyncio.sleep(2)

        explanation_escaped = escape_md(mcq["explanation"])
        spoiler = "💡 Explanation:\n\n||" + explanation_escaped + "||"
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=spoiler,
            parse_mode="MarkdownV2"
        )
        logger.info("Successfully posted to channel")
    except Exception as e:
        logger.error("Post to channel error: " + str(e))

# ======================
# SCHEDULED JOB
# ======================
async def scheduled_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        logger.info("Running scheduled job...")
        topic = await generate_topic()
        logger.info("Generated topic: " + topic)
        await asyncio.sleep(20)
        mcq = await generate_mcq(topic)
        if not mcq:
            logger.error("Failed to generate MCQ")
            return
        await send_for_approval(context.bot, mcq, "Auto: " + topic)
    except Exception as e:
        logger.error("Scheduled job error: " + str(e))

# ======================
# CALLBACK HANDLER
# ======================
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("approve_"):
        qid = data.replace("approve_", "")
        item = pending_questions.get(qid)
        if item:
            await post_to_channel(context.bot, item["mcq"])
            pending_questions.pop(qid, None)
            await query.edit_message_text("✅ Posted to channel!")
        else:
            await query.edit_message_text("❌ Question expired.")

    elif data.startswith("reject_"):
        qid = data.replace("reject_", "")
        pending_questions.pop(qid, None)
        await query.edit_message_text("❌ Rejected.")

    elif data.startswith("regen_"):
        qid = data.replace("regen_", "")
        pending_questions.pop(qid, None)
        await query.edit_message_text("🔄 Regenerating...")
        topic = await generate_topic()
        await asyncio.sleep(20)
        mcq = await generate_mcq(topic)
        if mcq:
            await send_for_approval(context.bot, mcq, "Regenerated: " + topic)
        else:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text="❌ Failed to regenerate. Try /postnow"
            )

# ======================
# IMAGE HANDLER
# ======================
async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        await update.message.reply_text("🖼️ Image received. Generating MCQ...")
        photo = update.message.photo[-1]
        file = await photo.get_file()
        image_bytes = await file.download_as_bytearray()
        mcq, preview = await generate_mcq_from_image(bytes(image_bytes))
        if not mcq:
            await update.message.reply_text(f"❌ Failed. Reason: {preview}")
            return
        await send_for_approval(context.bot, mcq, "Image Upload")
    except Exception as e:
        logger.error("Image handler error: " + str(e))
        await update.message.reply_text("❌ Image processing failed: " + str(e))

# ======================
# PDF HANDLER
# ======================
async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        await update.message.reply_text("📄 PDF received. Extracting text...")
        file = await update.message.document.get_file()
        file_bytes = await file.download_as_bytearray()
        pdf_reader = PyPDF2.PdfReader(io.BytesIO(bytes(file_bytes)))
        text = ""
        for page in pdf_reader.pages[:10]:
            extracted = page.extract_text()
            if extracted:
                text += extracted + "\n"
        if not text.strip():
            await update.message.reply_text("❌ Could not extract text.")
            return
        text = text[:4000]
        await update.message.reply_text("⏳ Generating MCQ from PDF...")
        await asyncio.sleep(20)
        mcq = await generate_mcq(text)
        if not mcq:
            await update.message.reply_text("❌ Failed to generate MCQ.")
            return
        await send_for_approval(context.bot, mcq, "PDF Upload")
    except Exception as e:
        logger.error("PDF error: " + str(e))
        await update.message.reply_text("❌ PDF processing failed.")

# ======================
# COMMANDS
# ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(f"Start command from user ID: {user_id}")
    if user_id != ADMIN_ID:
        await update.message.reply_text(f"⛔ Unauthorized. Your ID: {user_id}")
        return
    await update.message.reply_text(
        "✅ Pharma Quiz Bot Running!\n\n"
        "📚 Book Commands:\n"
        "/harper - MCQ from Harper Biochemistry 33rd Ed\n"
        "/goodman - MCQ from Goodman & Gilman\n\n"
        "🤖 Other Commands:\n"
        "/postnow - Generate auto topic MCQ\n"
        "/status - Check bot status\n"
        "/debug - Test bot connectivity\n\n"
        "📎 Send PDF or photo of textbook page → auto MCQ!"
    )

async def harper_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("📖 Generating MCQ from Harper's Biochemistry 33rd Edition...")
    topic = await generate_topic(book="harper")
    await update.message.reply_text(f"🧬 Topic: {topic}")
    await asyncio.sleep(20)
    mcq = await generate_mcq(topic, book_context="harper")
    if not mcq:
        await update.message.reply_text("❌ Failed to generate MCQ.")
        return
    await send_for_approval(context.bot, mcq, f"Harper 33e: {topic}")

async def goodman_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("💊 Generating MCQ from Goodman & Gilman...")
    topic = await generate_topic(book="goodman")
    await update.message.reply_text(f"💉 Topic: {topic}")
    await asyncio.sleep(20)
    mcq = await generate_mcq(topic, book_context="goodman")
    if not mcq:
        await update.message.reply_text("❌ Failed to generate MCQ.")
        return
    await send_for_approval(context.bot, mcq, f"Goodman & Gilman: {topic}")

async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text(
        f"🔧 Debug Info\n"
        f"Your ID: {user_id}\n"
        f"Admin ID: {ADMIN_ID}\n"
        f"Match: {'✅' if user_id == ADMIN_ID else '❌'}\n"
        f"Pending approvals: {len(pending_questions)}\n"
        f"Topics used: {len(used_topics)}"
    )

async def post_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("⏳ Generating MCQ... please wait 30-60 seconds")
    await scheduled_job(context)

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(
        "✅ Bot is running\n"
        "📊 Pending approvals: " + str(len(pending_questions)) + "\n"
        "📚 Topics used: " + str(len(used_topics))
    )

# ======================
# MAIN
# ======================
def main():
    logger.info("Starting Pharma Quiz Bot...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("postnow", post_now))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("harper", harper_command))
    app.add_handler(CommandHandler("goodman", goodman_command))
    app.add_handler(CommandHandler("debug", debug_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    app.add_handler(MessageHandler(filters.PHOTO, handle_image))

    app.job_queue.run_repeating(
        scheduled_job,
        interval=INTERVAL,
        first=30
    )

    logger.info("Bot started! Interval: " + str(INTERVAL // 60) + " minutes")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
