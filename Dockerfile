FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Persist the bot's memory (state/, dungeon/) on a Railway volume mounted at /app/state etc.
ENV PYTHONUNBUFFERED=1

# Shadow mode by default. Live execution stays hard-gated until the Gauntlet passes + armed.
CMD ["python", "-m", "earshot.main", "--loop"]
