import os
import json
import re
import time
import logging
import asyncio
import io

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
INTERVAL = 900

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

# ======================
# GENERATE TOPIC
# ======================
async def generate_topic():
   used = ", ".join(used_topics[-20:]) if used_topics else "none"
   prompt = f"""You are a NEET PG / FMGE / USMLE medical expert.

Suggest ONE specific high-yield topic for a biochemistry or pharmacology MCQ.

Already used topics (avoid repeating): {used}

Requirements:
- Must be specific (not just "pharmacology")
- Must be clinically relevant
- Alternate between biochemistry and pharmacology
- Focus on NEET PG high yield topics

Return ONLY JSON:
{{"topic": "Warfarin mechanism and vitamin K cycle"}}"""
   try:
       response = client.chat.completions.create(
           model="llama-3.3-70b-versatile",
           messages=[{"role": "user", "content": prompt}],
           temperature=0.9
       )
       result = extract_json(response.choices[0].message.content)
       topic = result.get("topic") if result else "Pharmacology high yield topic"
       used_topics.append(topic)
       if len(used_topics) > 100:
           used_topics.pop(0)
       return topic
   except Exception as e:
       logger.error(f"Topic generation error: {e}")
       return "Biochemistry high yield topic"

# ======================
# GENERATE MCQ
# ======================
async def generate_mcq(content):
   prompt = f"""You are a NEET PG / USMLE / FMGE expert examiner.

Generate ONE high-yield clinical MCQ based on: {content}

Rules:
- Clinical vignette style (patient scenario)
- 4 options, one definitively correct
- No ambiguous or trick questions
- Explanation must cite mechanism clearly
- Explain why each wrong option is incorrect
- NEET PG / FMGE exam standard

Return ONLY this JSON:
{{
 "question": "A patient presents with...",
 "options": ["A) ...", "B) ...", "C) ...", "D) ..."],
 "answer_index": 0,
 "explanation": "Correct: A because... B is wrong because... C is wrong because... D is wrong because..."
}}"""
   try:
       response = client.chat.completions.create(
           model="llama-3.3-70b-versatile",
           messages=[{"role": "user", "content": prompt}],
           temperature=0.3
       )
       return extract_json(response.choices[0].message.content)
   except Exception as e:
       logger.error(f"MCQ generation error: {e}")
       return None

# ======================
# VALIDATE MCQ
# ======================
async def validate_mcq(mcq):
   prompt = f"""You are a medical education quality reviewer.

Review this MCQ for accuracy and quality:

Question: {mcq['question']}
Options: {mcq['options']}
Answer index: {mcq['answer_index']}
Explanation: {mcq['explanation']}

Check:
1. Is the correct answer medically accurate?
2. Is the explanation accurate and detailed?
3. Are wrong options clearly incorrect?
4. Is it high yield for NEET PG / FMGE?
5. Is the clinical vignette realistic?

Return ONLY JSON:
{{
 "score": 8,
 "is_accurate": true,
 "feedback": "Question is accurate and high yield"
}}

Score 1-10. Be strict."""
   try:
       response = client.chat.completions.create(
           model="llama-3.3-70b-versatile",
           messages=[{"role": "user", "content": prompt}],
           temperature=0.1
       )
       return extract_json(response.choices[0].message.content)
   except Exception as e:
       logger.error(f"Validation error: {e}")
       return None

# ======================
# SEND FOR APPROVAL
# ======================
async def send_for_approval(context, mcq, source):
   qid = str(int(time.time()))
   pending_questions[qid] = {"mcq": mcq, "source": source}

   correct_option = mcq["options"][mcq["answer_index"]]

   text = (
       f"📋 *NEW MCQ FOR APPROVAL*\n\n"
       f"📚 Source: {source}\n\n"
       f"*{mcq['question']}*\n\n"
       + "\n".join(mcq["options"]) +
       f"\n\n✅ *Correct: {correct_option}*\n\n"
       f"💡 *Explanation:*\n{mcq['explanation']}"
   )

   keyboard = InlineKeyboardMarkup([
       [
           InlineKeyboardButton("✅ Approve & Post", callback_data=f"approve_{qid}"),
           InlineKeyboardButton("❌ Reject", callback_data=f"reject_{qid}")
       ],
       [
           InlineKeyboardButton("🔄 Regenerate", callback_data=f"regen_{qid}")
       ]
   ])

   await context.bot.send_message(
       chat_id=ADMIN_ID,
       text=text,
       parse_mode="Markdown",
       reply_markup=keyboard
   )

# ======================
# POST TO CHANNEL
# ======================
async def post_to_channel(context, mcq):
   try:
       text_msg = (
           f"📚 *DAILY MCQ*\n\n"
           f"*{mcq['question']}*\n\n"
           + "\n".join(mcq["options"])
       )
       await context.bot.send_message(
           chat_id=CHANNEL_ID,
           text=text_msg,
           parse_mode="Markdown"
       )
       await asyncio.sleep(2)

       clean_options = []
       for opt in mcq["options"]:
           if len(opt) > 2 and opt[1] == ")":
               clean_options.append(opt[3:].strip())
           else:
               clean_options.append(opt)

       await context.bot.send_poll(
           chat_id=CHANNEL_ID,
           question=mcq["question"][:300],
           options=clean_options,
           type="quiz",
           correct_option_id=int(mcq["answer_index"]),
           is_anonymous=True
       )
       await asyncio.sleep(2)

       explanation_escaped = escape_md(mcq["explanation"])
       spoiler = f"💡 *Explanation:*\n\n||{explanation_escaped}||"
       await context.bot.send_message(
           chat_id=CHANNEL_ID,
           text=spoiler,
           parse_mode="MarkdownV2"
       )
       logger.info("Successfully posted to channel")
   except Exception as e:
       logger.error(f"Post to channel error: {e}")

# ======================
# SCHEDULED JOB
# ======================
async def scheduled_job(context: ContextTypes.DEFAULT_TYPE):
   try:
       topic = await generate_topic()
       logger.info(f"AI generated topic: {topic}")

       mcq = await generate_mcq(topic)
       if not mcq:
           logger.error("Failed to generate MCQ")
           return

       review = await validate_mcq(mcq)
       score = review.get("score", 0) if review else 0
       logger.info(f"MCQ score: {score}")

       if score >= 7:
           await send_for_approval(context, mcq, f"Auto: {topic}")
       else:
           logger.info(f"Low score ({score}), regenerating...")
           topic2 = await generate_topic()
           mcq2 = await generate_mcq(topic2)
           if mcq2:
               await send_for_approval(context, mcq2, f"Auto retry: {topic2}")
   except Exception as e:
       logger.error(f"Scheduled job error: {e}")

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
           await post_to_channel(context, item["mcq"])
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
       await query.edit_message_text("🔄 Regenerating new question...")
       topic = await generate_topic()
       mcq = await generate_mcq(topic)
       if mcq:
           await send_for_approval(context, mcq, f"Regener
