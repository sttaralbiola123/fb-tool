from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import requests
import os
import threading
import time
from supabase import create_client, Client

app = Flask(__name__, static_folder='static')
CORS(app)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://ilpvxzclptsthajvvltl.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImlscHZ4emNscHRzdGhhanZ2bHRsIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4MTA4NjI0MywiZXhwIjoyMDk2NjYyMjQzfQ.ZgCrh3K-a9GLjVndCDsG37rXIZxZwQcs_Z6guMfp13E")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def get_fb_user_id(token):
    try:
        # Try v2.0 first
        res = requests.get(f"https://graph.facebook.com/v2.0/me?access_token={token}&fields=id,name", timeout=10)
        data = res.json()
        if data.get("id"):
            return data.get("id"), data.get("name")
        # Try latest version
        res = requests.get(f"https://graph.facebook.com/me?access_token={token}&fields=id,name", timeout=10)
        data = res.json()
        return data.get("id"), data.get("name")
    except:
        return None, None

def extract_post_id(post_url):
    """Extract post ID from Facebook URL for Graph API"""
    try:
        if "story_fbid=" in post_url:
            story_id = post_url.split("story_fbid=")[1].split("&")[0]
            page_id = post_url.split("id=")[1].split("&")[0]
            return f"{page_id}_{story_id}"
        elif "/posts/" in post_url:
            parts = post_url.split("/posts/")
            user = parts[0].split("/")[-1]
            post = parts[1].split("/")[0].split("?")[0]
            return f"{user}_{post}"
        elif "pfbid" in post_url:
            # newer fb URL format, return as-is for now
            return post_url
        return None
    except:
        return None

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/api/verify', methods=['POST'])
def verify_token():
    data = request.json
    token = data.get('token', '').strip()

    if not token:
        return jsonify({"success": False, "message": "No token provided"}), 400

    user_id, name = get_fb_user_id(token)
    if not user_id:
        return jsonify({"success": False, "message": "Invalid or expired token"}), 400

    # Check if already registered
    existing = supabase.table("tokens").select("*").eq("fb_user_id", user_id).execute()

    if existing.data:
        # Update token
        supabase.table("tokens").update({
            "access_token": token
        }).eq("fb_user_id", user_id).execute()
        return jsonify({"success": True, "message": f"Welcome back, {name}!", "user_id": user_id, "name": name})
    else:
        # Insert new user
        supabase.table("tokens").insert({
            "access_token": token,
            "fb_user_id": user_id,
            "likes_given": 0,
            "likes_received": 0
        }).execute()
        return jsonify({"success": True, "message": f"Registered successfully, {name}!", "user_id": user_id, "name": name})

@app.route('/api/submit', methods=['POST'])
def submit_post():
    data = request.json
    token = data.get('token', '').strip()
    post_url = data.get('post_url', '').strip()

    if not token or not post_url:
        return jsonify({"success": False, "message": "Token and post URL required"}), 400

    user_id, name = get_fb_user_id(token)
    if not user_id:
        return jsonify({"success": False, "message": "Invalid token"}), 400

    post_id = extract_post_id(post_url)

    # Start liking in background
    threading.Thread(target=distribute_likes, args=(token, user_id, post_url, post_id)).start()

    return jsonify({"success": True, "message": "Post submitted! Sending likes now...", "estimated": "50-200 likes"})

def distribute_likes(requester_token, requester_user_id, post_url, post_id):
    """Get all tokens from DB and use them to like the post"""
    try:
        all_tokens = supabase.table("tokens").select("*").execute()

        liked_count = 0
        for row in all_tokens.data:
            token = row["access_token"]
            liker_user_id = row["fb_user_id"]

            if liker_user_id == requester_user_id:
                continue  # Skip own token

            try:
                # Try to like using Graph API
                if post_id and "_" in str(post_id):
                    res = requests.post(
                        f"https://graph.facebook.com/{post_id}/likes",
                        params={"access_token": token},
                        timeout=10
                    )
                    if res.status_code == 200:
                        liked_count += 1
                        # Update likes_given for liker
                        supabase.table("tokens").update({
                            "likes_given": row["likes_given"] + 1
                        }).eq("fb_user_id", liker_user_id).execute()
                        time.sleep(0.5)  # Rate limit protection
            except:
                continue

        # Update likes_received for requester
        requester = supabase.table("tokens").select("*").eq("fb_user_id", requester_user_id).execute()
        if requester.data:
            current = requester.data[0].get("likes_received", 0)
            supabase.table("tokens").update({
                "likes_received": current + liked_count
            }).eq("fb_user_id", requester_user_id).execute()

    except Exception as e:
        print(f"Error distributing likes: {e}")

@app.route('/api/stats', methods=['POST'])
def get_stats():
    data = request.json
    token = data.get('token', '').strip()

    user_id, _ = get_fb_user_id(token)
    if not user_id:
        return jsonify({"success": False, "message": "Invalid token"}), 400

    row = supabase.table("tokens").select("*").eq("fb_user_id", user_id).execute()
    if row.data:
        return jsonify({
            "success": True,
            "likes_given": row.data[0].get("likes_given", 0),
            "likes_received": row.data[0].get("likes_received", 0),
            "total_users": len(supabase.table("tokens").select("fb_user_id").execute().data)
        })
    return jsonify({"success": False, "message": "User not found"}), 404

@app.route('/api/total_users', methods=['GET'])
def total_users():
    res = supabase.table("tokens").select("fb_user_id").execute()
    return jsonify({"count": len(res.data)})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
            
