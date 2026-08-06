import os
import sqlite3
from pathlib import Path
from google import genai
from moviepy import VideoFileClip, AudioFileClip
import moviepy.audio.fx as afx

# Initialize Gemini Client securely via Environment Variable
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY environment variable not set. Please set it in your system or GitHub Secrets.")

client = genai.Client(api_key=GEMINI_API_KEY)

# Directory Configuration
BASE_DIR = Path(__file__).parent
INPUT_DIR = BASE_DIR / "input"
OUTPUT_DIR = BASE_DIR / "output"
MUSIC_DIR = BASE_DIR / "music"
DB_PATH = BASE_DIR / "database.db"

# Create essential folders if they don't exist
OUTPUT_DIR.mkdir(exist_ok=True)
INPUT_DIR.mkdir(exist_ok=True)
MUSIC_DIR.mkdir(exist_ok=True)

SHORT_DURATION_SEC = 50.0


# --- DATABASE & MOVIE SELECTION ---

def init_db():
    """Initializes SQLite database table to store current rendering progress."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS active_movie (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL,
            movie_name TEXT NOT NULL,
            current_timestamp_sec REAL NOT NULL,
            end_timestamp_sec REAL NOT NULL,
            current_part INTEGER NOT NULL,
            music_index INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


def get_or_create_active_movie_state():
    """Fetches active processing state or selects the oldest movie from input/ folder."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT file_path, movie_name, current_timestamp_sec, end_timestamp_sec, current_part, music_index 
        FROM active_movie LIMIT 1
    """)
    row = cursor.fetchone()

    if row:
        conn.close()
        return {
            "file_path": Path(row[0]),
            "movie_name": row[1],
            "current_timestamp": row[2],
            "end_timestamp": row[3],
            "current_part": row[4],
            "music_index": row[5]
        }

    # Find valid video files in input/ folder
    valid_extensions = (".mp4", ".mkv", ".avi", ".mov")
    movie_files = [f for f in INPUT_DIR.iterdir() if f.suffix.lower() in valid_extensions]

    if not movie_files:
        conn.close()
        raise FileNotFoundError("No movie files found in 'input/' directory.")

    # Sort movies by creation time to pick the oldest uploaded file first
    movie_files.sort(key=lambda f: f.stat().st_ctime)
    chosen_movie = movie_files[0]

    with VideoFileClip(str(chosen_movie)) as video:
        total_duration = video.duration

    start_timestamp = 60.0  # Skip intro (60 seconds)
    end_timestamp = total_duration - 120.0  # End before credits (2 mins remaining)

    # Clean file name for titles
    movie_name = chosen_movie.stem.replace(".", " ").replace("_", " ").title()

    cursor.execute("""
        INSERT INTO active_movie (file_path, movie_name, current_timestamp_sec, end_timestamp_sec, current_part, music_index)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (str(chosen_movie), movie_name, start_timestamp, end_timestamp, 1, 0))

    conn.commit()
    conn.close()

    return {
        "file_path": chosen_movie,
        "movie_name": movie_name,
        "current_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "current_part": 1,
        "music_index": 0
    }


def update_movie_state(next_timestamp, next_part, next_music_index, is_final_part):
    """Updates SQLite state with the next rendering position."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    if is_final_part:
        # Clear database entry so the script picks up the next movie on next execution
        cursor.execute("DELETE FROM active_movie")
        print("Completed entire movie. Cleared active state from database.")
    else:
        cursor.execute("""
            UPDATE active_movie 
            SET current_timestamp_sec = ?, current_part = ?, music_index = ?
            WHERE id = (SELECT id FROM active_movie LIMIT 1)
        """, (next_timestamp, next_part, next_music_index))
        print(f"Saved progress: Next Start = {next_timestamp:.2f}s | Next Part = {next_part}")

    conn.commit()
    conn.close()


# --- AI TITLE GENERATION & AUDIO SELECTION ---

def generate_title(movie_name, part_num):
    """Uses Gemini API to produce an engaging YouTube Short title."""
    prompt = f"Generate 1 short viral YouTube Short title for a movie recap of '{movie_name}', Part {part_num}. Keep it under 60 characters with relevant emojis and #shorts."
    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=prompt
    )
    return response.text.strip()


def get_sequential_music_clip(target_duration, current_index):
    """Selects sequential background music from music/ folder and loops it cleanly."""
    valid_audio_extensions = (".mp3", ".wav", ".aac", ".m4a")
    music_files = [f for f in MUSIC_DIR.iterdir() if f.suffix.lower() in valid_audio_extensions]

    if not music_files:
        raise FileNotFoundError(f"No audio files found in '{MUSIC_DIR}'. Place MP3s there.")

    music_files.sort(key=lambda f: f.name.lower())
    selected_index = current_index % len(music_files)
    chosen_sound = music_files[selected_index]

    audio_clip = AudioFileClip(str(chosen_sound))

    # MoviePy v2 audio loop
    if audio_clip.duration < target_duration:
        audio_clip = afx.AudioLoop(duration=target_duration).apply(audio_clip)
    else:
        audio_clip = audio_clip.subclipped(0, target_duration)

    next_index = (selected_index + 1) % len(music_files)
    return audio_clip, next_index


# --- VIDEO PROCESSING ---

def render_daily_short(state):
    """Crops video to 9:16 vertical ratio and exports high quality YouTube Short."""
    movie_path = state["file_path"]
    start_time = state["current_timestamp"]
    end_limit = state["end_timestamp"]
    part_num = state["current_part"]
    movie_name = state["movie_name"]
    music_index = state["music_index"]

    print(f"--- Processing {movie_name} | Part {part_num} ---")

    viral_title = generate_title(movie_name, part_num)

    clip_duration = SHORT_DURATION_SEC
    next_timestamp = start_time + clip_duration

    is_final_part = False
    if next_timestamp >= end_limit:
        clip_duration = end_limit - start_time
        next_timestamp = end_limit
        is_final_part = True

    bg_music, next_music_index = get_sequential_music_clip(clip_duration, music_index)

    with VideoFileClip(str(movie_path)) as video:
        subclip = video.subclipped(start_time, start_time + clip_duration).without_audio()

        # Center-crop 16:9 widescreen to 9:16 vertical video (No black bars)
        w, h = subclip.size
        target_w = int(h * (9 / 16))
        x_center = w / 2

        cropped = subclip.cropped(
            x1=x_center - target_w / 2,
            y1=0,
            x2=x_center + target_w / 2,
            y2=h
        )

        final_short = (
            cropped.resized(newsize=(1080, 1920))
            .with_audio(bg_music)
            .with_duration(clip_duration)
        )

        output_file = OUTPUT_DIR / f"{movie_path.stem}_Part_{part_num}.mp4"

        final_short.write_videofile(
            str(output_file),
            fps=30,
            codec="libx264",
            audio_codec="aac",
            bitrate="8000k",
            preset="medium"
        )

    bg_music.close()
    return output_file, viral_title, next_timestamp, next_music_index, is_final_part


# --- MAIN PIPELINE EXECUTION ---

def run_daily_job():
    """Runs database initialization, renders short video, and updates state."""
    init_db()
    state = get_or_create_active_movie_state()

    output_video, viral_title, next_timestamp, next_music_index, is_final_part = render_daily_short(state)

    print(f"Successfully generated: {output_video.name}")
    print(f"Generated YouTube Title: {viral_title}")

    update_movie_state(
        next_timestamp=next_timestamp,
        next_part=state["current_part"] + 1,
        next_music_index=next_music_index,
        is_final_part=is_final_part
    )


if __name__ == "__main__":
    run_daily_job()
  
