import sqlite3
import hashlib
import random
from urllib.parse import quote
import string
import requests
import datetime
import time
import os
from functools import wraps
from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    render_template,
    request,
    redirect,
    url_for,
    session,
    g,
    jsonify,
)
from flask_cors import CORS, cross_origin
from flask_session import Session
from flask_sitemapper import Sitemapper
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import helpers
import database_setup

load_dotenv()
SIGHT_ENGINE_SECRET = os.getenv("SIGHT_ENGINE_SECRET")

app = Flask(__name__)
app.secret_key = "super secret key"
cors = CORS(app)
app.config["CORS_HEADERS"] = "Content-Type"

sitemapper = Sitemapper()
sitemapper.init_app(app)

# Rate limiting
limiter = Limiter(get_remote_address, app=app)

# Set up the session object
app.config["SESSION_PERMANENT"] = False
app.config["SESSION_TYPE"] = "filesystem"
Session(app)

DATABASE = "tweetor.db"

staff_accounts = ["ItsMe", "Dude_Pog"]

@app.template_filter('username_trim')
def trim_username(s, n):
    if len(s) <= n:
        return s
    return s[:n-3]+'...'

def get_engaged_direct_messages(user_handle):
    db = helpers.get_db()
    cursor = db.cursor()

    cursor.execute(
        """
        SELECT DISTINCT receiver_handle FROM direct_messages
        WHERE sender_handle = ?
        UNION
        SELECT DISTINCT sender_handle FROM direct_messages
        WHERE receiver_handle = ?
    """,
        (user_handle, user_handle),
    )

    engaged_dms = cursor.fetchall()

    db.commit()

    return engaged_dms

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'username' not in session:
            return redirect(url_for('login'), 302)
        return f(*args, **kwargs)
    return decorated_function

@sitemapper.include()
@app.route("/")
def home() -> Response:
    # Get a connection to the database
    db = helpers.get_db()

    # Create a cursor to interact with the database
    cursor = db.cursor()

    # Check if the user is logged in and is an admin
    if "username" in session and session["handle"] == "admin":
        # If admin, retrieve all flits regardless of content
        cursor.execute("SELECT * FROM flits ORDER BY timestamp DESC")
    else:
        # If not admin, retrieve only non-profane flits
        cursor.execute(
            "SELECT * FROM flits WHERE profane_flit = 'no' ORDER BY timestamp DESC"
        )

    # Fetch the results of the SQL query
    flits = cursor.fetchall()

    # Check if the user is logged in
    if "username" in session:
        # Get the user's handle from the session
        user_handle = session["handle"]

        # Get the list of engaged direct messages for the user
        engaged_dms = get_engaged_direct_messages(user_handle)

        # Render the home template with user-specific data
        return render_template(
            "home.html",
            flits=flits,
            loggedIn=True,
            engaged_dms=engaged_dms,
        )
    else:
        # Render the home template without user-specific data since not logged in
        return render_template("home.html", flits=flits, loggedIn=False)

@app.route("/api/get_flits")
def get_flits():
    skip = request.args.get("skip")
    limit = request.args.get("limit")

    # Get a connection to the database
    db = helpers.get_db()

    # Create a cursor to interact with the database
    cursor = db.cursor()
    try:
        limit = int(request.args.get("limit"))
        skip = int(request.args.get("skip"))
    except ValueError:
        # Handle the error, e.g., return an error response or set default values
        limit = 10
        skip = 0

    cursor.execute("SELECT * FROM flits WHERE profane_flit = 'no' ORDER BY id DESC LIMIT ? OFFSET ?", (limit, skip))
    
    return jsonify([dict(flit) for flit in cursor.fetchall()])


@app.route("/submit_flit", methods=["POST"])
@limiter.limit("4/minute")
def submit_flit() -> Response:
    # Get a connection to the database
    db = helpers.get_db()

    # Create a cursor to interact with the database
    cursor = db.cursor()

    # Check if the original_flit_id field is present in the form data
    if request.form.get("original_flit_id") is None:
        # Extract form data for the new flit
        content = str(request.form["content"])
        meme_url = request.form["meme_link"]

        # Validate meme URL format
        if not meme_url.startswith("https://media.tenor.com/") and meme_url != "":
            return render_template(
                "error.html", error="Why is this meme not from tenor?"
            )

        # Check if the user is muted
        if session.get("username") in muted:
            return render_template("error.html", error="You were muted.")

        # Check for various content validation conditions
        if content.strip() == "":
            return render_template("error.html", error="Message was blank.")
        if len(content) > 280:
            return render_template("error.html", error="Message was too long.")
        if "username" not in session:
            return render_template("error.html", error="You are not logged in.")

        # Extract and validate hashtag from form data
        hashtag = request.form["hashtag"]

        # Use the Sightengine result to check for profanity
        sightengine_result = is_profanity(content)
        profane_flit = "no"
        if (
            sightengine_result["status"] == "success"
            and len(sightengine_result["profanity"]["matches"]) > 0
        ):
            profane_flit = "yes"
            return render_template(
                "error.html", error="Do you really think that's appropriate?"
            )

        # Insert the new flit into the database
        cursor.execute(
            "INSERT INTO flits (username, content, userHandle, hashtag, profane_flit, meme_link, is_reflit, original_flit_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session["username"],
                content,
                session["handle"],
                hashtag,
                profane_flit,
                meme_url,
                0,
                -1,
            ),
        )
        db.commit()
        db.close()
        return redirect(url_for("home"))

    # Check for reflit
    is_reflit = False
    original_flit_id = request.form.get("original_flit_id")  # Get original_flit_id from form data
    if original_flit_id is not None:
        # Look for the original flit in the database
        cursor.execute("SELECT id FROM flits WHERE id = ?", (original_flit_id,))
        original_flit = cursor.fetchone()

        if original_flit:  # If the original flit exists
            is_reflit = True
            # Instead of using form content as new flit content, indicate it's a reflit
            content = "Reflit: " + str(original_flit_id)

    # Insert the reflit or empty flit into the database
    cursor.execute(
        "INSERT INTO flits (username, content, userHandle, hashtag, profane_flit, meme_link, is_reflit, original_flit_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session["username"],
            "",
            session["handle"],
            "",
            "no",
            "",
            int(is_reflit),
            original_flit_id,
        ),
    )

    db.commit()
    db.close()
    return redirect(url_for("home"))


used_captchas = []

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    if "username" not in session:
        return render_template('error.html', error="Are you signed in?")
    return render_template('settings.html',
        loggedIn=("username" in session),
        engaged_dms=[]
        if "username" not in session
        else get_engaged_direct_messages(session["username"])
    )

# Signup route
@sitemapper.include()
@app.route("/signup", methods=["GET", "POST"])
def signup() -> Response:
    error = None

    # If the HTTP request method is POST, handle form submission
    if request.method == "POST":
        username = request.form["username"].strip()
        handle = username
        password = request.form["password"]
        passwordConformation = request.form["passwordConformation"]
        user_captcha_input = request.form["input"]
        correct_captcha = request.form["correct_captcha"]

        # Prevent spam by checking if the captcha was already used
        if correct_captcha in used_captchas:
            return "This captcha has already been used. Try to refresh the captcha."
        used_captchas.append(correct_captcha)

        # Check if the user-provided captcha input matches the correct captcha
        if user_captcha_input != correct_captcha:
            return redirect("/signup")

        # Check if the provided passwords match
        if password != passwordConformation:
            return redirect("/signup")
        
        # Check if the username has bad characters
        if "|" in username:
            return "Usernames cannot contain |"

        # Get a connection to the database
        db = helpers.get_db()

        # Create a cursor to interact with the database
        cursor = db.cursor()

        # Check if the username already exists in the database
        cursor.execute("SELECT * FROM users WHERE username = ?", (username,))
        
        # If the username is taken, modify the handle to make it unique
        if len(cursor.fetchall()) != 0:
            handle = f"{username}{len(cursor.fetchall())}"

        # Hash the password before storing it in the database
        hashed_password = hashlib.sha256(password.encode()).hexdigest()

        # Insert the new user data into the database
        cursor.execute(
            "INSERT INTO users (username, password, handle, turbo) VALUES (?, ?, ?, ?)",
            (username, hashed_password, handle, 0),
        )
        db.commit()
        db.close()

        # Set session data for the newly registered user
        session["handle"] = handle
        session["username"] = username

        # Redirect to the home page
        return redirect("/")

    # If the user is already logged in, redirect to the home page
    if "username" in session:
        return redirect("/")

    # Render the signup template with potential error messages
    return render_template("signup.html", error=error)


# Login route
@sitemapper.include()
@app.route("/login", methods=["GET", "POST"])
def login() -> Response:
    # Handle form submission if the request method is POST
    if request.method == "POST":
        handle = request.form["handle"]
        password = request.form["password"]

        # Get a connection to the database
        db = helpers.get_db()

        # Create a cursor to interact with the database
        cursor = db.cursor()

        # Query the database for the user with the provided handle
        cursor.execute("SELECT * FROM users WHERE handle = ?", (handle,))
        users = cursor.fetchall()

        # If there is no or more than one matching user, redirect to the login page
        if len(users) != 1:
            return redirect("/login")

        # Hash the provided password to check against the stored hashed password
        hashed_password = hashlib.sha256(password.encode()).hexdigest()

        # If the password matches the stored hashed password, set session data for the user
        if users[0]["password"] == hashed_password:
            session["handle"] = handle
            session["username"] = users[0]["username"]
        else:
            # If the password doesn't match, redirect to the login page
            return redirect("/login")

        # Redirect the user to the home page after successful login
        return redirect("/")

    # If the user is already logged in, redirect to the home page
    if "username" in session:
        return redirect("/")

    # Render the login template for users who are not logged in
    return render_template("login.html")

@app.route('/change_password', methods=['GET', 'POST'])
@login_required
def change_password():
    if request.method == 'POST':
        current_password = request.form['current_password']
        new_password = request.form['new_password']

        db = helpers.get_db()
        cursor = db.cursor()
        cursor.execute("SELECT password FROM users WHERE username = ?", (session["username"],))
        user = cursor.fetchone()

        hashed_password = hashlib.sha256(current_password.encode()).hexdigest()

        if user['password'] == hashed_password:
            new_hashed_password = hashlib.sha256(new_password.encode()).hexdigest()
            cursor.execute(
                "UPDATE users SET password = ? WHERE username = ?",
                (new_hashed_password, session["username"]),
            )
            db.commit()
            return redirect('/')
        else:
            return 'Current password is incorrect'

    return render_template('change_password.html')


@app.route('/leaderboard')
def leaderboard():
    return render_template('leaderboard.html',
        loggedIn=("username" in session),
        engaged_dms=[]
        if "username" not in session
        else get_engaged_direct_messages(session["username"])
    )

c = sqlite3.connect(DATABASE).cursor()


def get_all_flit_ids():
    c.execute("SELECT id FROM flits")
    flit_ids = [i[0] for i in c.fetchall()]
    return flit_ids


@sitemapper.include(url_variables={"flit_id": get_all_flit_ids()})
@app.route("/flits/<flit_id>")
def singleflit(flit_id: str) -> Response:
    # Get a connection to the database
    conn = helpers.get_db()

    # Create a cursor to interact with the database
    c = conn.cursor()

    # Retrieve the specified flit's information from the database
    c.execute("SELECT * FROM flits WHERE id=?", (flit_id,))
    flit = c.fetchone()

    if flit:
        original_flit = None
        if flit["is_reflit"] == 1:
            # Retrieve the original flit's information if this flit is a reflit
            c.execute("SELECT * FROM flits WHERE id = ?", (flit["original_flit_id"],))
            original_flit = c.fetchone()

        # Render the template with the flit's information
        return render_template(
            "flit.html",
            flit=flit,
            loggedIn=("username" in session),
            original_flit=original_flit,
            engaged_dms=[]
            if "username" not in session
            else get_engaged_direct_messages(session["username"]),
        )

    # If the flit doesn't exist, redirect to the home page
    return redirect("/")

@app.route("/logout", methods=["GET", "POST"])
def logout() -> Response:
    # Check if the user is logged in
    if "username" in session:
        # Remove session data for the user
        session.pop("handle", None)
        session.pop("username", None)
    
    # Redirect the user to the home page, whether they were logged in or not
    return redirect("/")


def get_all_user_handles():
    c.execute("SELECT handle FROM users")
    user_handles = [i[0] for i in c.fetchall()]
    return user_handles


@sitemapper.include(url_variables={"username": get_all_user_handles()})
@app.route("/user/<path:username>")
def user_profile(username: str) -> Response:
    # Get a connection to the database
    conn = helpers.get_db()

    # Create a cursor to interact with the database
    cursor = conn.cursor()

    # Query the database for the user profile with the specified username
    cursor.execute("SELECT * FROM users WHERE handle = ?", (username,))
    user = cursor.fetchone()

    # If the user doesn't exist, redirect to the home page
    if not user:
        return redirect("/")

    # Query the database for the user's non-reflit flits, ordered by timestamp
    cursor.execute(
        "SELECT * FROM flits WHERE userHandle = ? ORDER BY timestamp DESC",
        (username,),
    )
    flits = cursor.fetchall()

    # Check if the logged-in user is following this user's profile
    is_following = False
    if "username" in session:
        logged_in_username = session["username"]
        cursor.execute(
            "SELECT * FROM follows WHERE followerHandle = ? AND followingHandle = ?",
            (logged_in_username, user["handle"]),
        )
        is_following = cursor.fetchone() is not None

    # Calculate the user's activeness based on their tweet frequency
    latest_tweet_time = datetime.datetime.now()
    first_tweet_time = flits[-1]["timestamp"]
    first_tweet_time = datetime.datetime.strptime(first_tweet_time, "%Y-%m-%d %H:%M:%S")
    diff = latest_tweet_time - first_tweet_time
    weeks = diff.total_seconds() / 3600 / 24 / 7
    activeness = round(0 if weeks == 0 else len(flits) / weeks * 1000)

    # Initialize a list for user badges
    badges = []

    # Add badges based on activeness and staff status
    if activeness > 5000:
        badges.append(("badges/creator.png", "Activeness of over 5000"))

    if user["handle"] in staff_accounts:
        badges.append(("badges/staff.png", "Staff at Tweetor!"))

    # Render the user profile template with relevant data
    return render_template(
        "user.html",
        badges=badges,
        user=user,
        loggedIn=("username" in session),
        flits=flits,
        is_following=is_following,
        activeness=activeness,
        engaged_dms=[]
        if "username" not in session
        else get_engaged_direct_messages(session["username"]),
    )

@app.route("/profanity")
def profanity() -> Response:
    if "username" in session and session["handle"] != "admin":
        return render_template(
            "error.html", error="You are not authorized to view this page."
        )

    db = helpers.get_db()
    cursor = db.cursor()
    cursor.execute(
        "SELECT * FROM flits WHERE profane_flit = 'yes' ORDER BY timestamp DESC"
    )
    profane_flit = cursor.fetchall()
    cursor.execute(
        """
        SELECT * FROM direct_messages WHERE profane_dm = "yes"
    """
    )
    profane_dm = cursor.fetchall()

    return render_template(
        "profanity.html", profane_flit=profane_flit, profane_dm=profane_dm
    )

def is_profanity(text):
    api_user = "570595698"
    api_secret = SIGHT_ENGINE_SECRET
    api_url = f"https://api.sightengine.com/1.0/text/check.json"

    data = {
        "text": text,
        "lang": "en",
        "mode": "standard",
        "api_user": api_user,
        "api_secret": api_secret,
        "categories": "drug,medical,extremism,weapon",
    }

    response = requests.post(api_url, data=data)
    result = response.json()

    return result  # Return the result instead of an empty list


@app.route("/delete_flit", methods=["GET"])
def delete_flit() -> Response:
    if "username" in session and session["handle"] != "admin":
        return render_template(
            "error.html", error="You are not authorized to perform this action."
        )

    flit_id = request.args.get("flit_id")
    db = helpers.get_db()
    cursor = db.cursor()
    cursor.execute("DELETE FROM flits WHERE id = ?", (flit_id,))
    cursor.execute("DELETE FROM reported_flits WHERE flit_id=?", (flit_id,))
    db.commit()

    return redirect(url_for("reported_flits"))


@app.route("/delete_user", methods=["POST"])
def delete_user() -> Response:
    if "username" in session and session["handle"] != "admin":
        return render_template(
            "error.html", error="You are not authorized to perform this action."
        )

    user_handle = request.form["user_handle"]
    db = helpers.get_db()
    cursor = db.cursor()
    cursor.execute("DELETE FROM users WHERE handle = ?", (user_handle,))
    db.commit()

    return redirect(url_for("home"))


@app.route("/report_flit", methods=["POST"])
def report_flit():
    flit_id = request.form["flit_id"]
    reporter_handle = session["handle"]
    reason = request.form["reason"]

    db = helpers.get_db()
    cursor = db.cursor()
    cursor.execute(
        "INSERT INTO reported_flits (flit_id, reporter_handle, reason) VALUES (?, ?, ?)",
        (flit_id, reporter_handle, reason),
    )
    db.commit()

    return redirect(url_for("home"))


@app.route("/reported_flits")
def reported_flits():
    if "username" in session and session["handle"] != "admin":
        return render_template(
            "error.html", error="You don't have permission to access this page."
        )

    db = helpers.get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM reported_flits")
    reports = cursor.fetchall()

    return render_template("reported_flits.html", reports=reports)


@app.route("/dm/<path:receiver_handle>")
def direct_messages(receiver_handle):
    if "username" not in session:
        return render_template("error.html", error="You are not logged in.")

    sender_handle = session["handle"]

    db = helpers.get_db()
    cursor = db.cursor()

    cursor.execute(
        """
        SELECT * FROM direct_messages
        WHERE (sender_handle = ? AND receiver_handle = ?)
        OR (sender_handle = ? AND receiver_handle = ?) AND profane_dm = 'no'
        ORDER BY timestamp DESC
    """,
        (sender_handle, receiver_handle, receiver_handle, sender_handle),
    )

    messages = cursor.fetchall()

    return render_template(
        "direct_messages.html",
        messages=messages,
        receiver_handle=receiver_handle,
        loggedIn="username" in session,
        engaged_dms=[]
        if "username" not in session
        else get_engaged_direct_messages(session["username"]),
    )


@app.route("/submit_dm/<path:receiver_handle>", methods=["POST"])
def submit_dm(receiver_handle):
    if "username" not in session:
        return render_template("error.html", error="You are not logged in.")

    sender_handle = session["handle"]
    content = request.form["content"]

    if len(content) > 1000:
        return render_template("error.html", error="Too many characters in DM")

    sightengine_result = is_profanity(content)
    profane_dm = "no"

    if (
        sightengine_result["status"] == "success"
        and len(sightengine_result["profanity"]["matches"]) > 0
    ):
        profane_dm = "yes"

    db = helpers.get_db()
    cursor = db.cursor()

    cursor.execute(
        """
        INSERT INTO direct_messages (sender_handle, receiver_handle, content, profane_dm)
        VALUES (?, ?, ?, ?)
    """,
        (sender_handle, receiver_handle, content, profane_dm),
    )

    db.commit()

    return redirect(
        url_for(
            "direct_messages",
            receiver_handle=receiver_handle,
            loggedIn="username" in session,
        )
    )

# Muting and unmuting

muted = []

@app.route("/mute/<handle>")
def mute(handle):
    if session.get("handle") == "admin":
        muted.append(handle)
        return "Completed"

@app.route("/unmute/<handle>")
def unmute(handle):
    if session.get("handle") == "admin":
        muted.remove(handle)
        return "Completed"

@app.route("/get_captcha")
def get_captcha():
    while True:
        correct_captcha = "".join(
            random.choices(
                string.ascii_uppercase + string.ascii_lowercase + string.digits, k=5
            )
        )
        if correct_captcha not in used_captchas:
            break
    return correct_captcha

@app.route("/api/flit")
def flitAPI():
    try:
        flit_id = int(request.args.get("flit_id"))
    except ValueError:
        return jsonify("Flit ID is invalid")
    db = helpers.get_db()
    c = db.cursor()
    c.execute('SELECT * FROM flits WHERE id=?', (flit_id,))
    flit = c.fetchone()

    if flit is None:
        return "profane"
    
    if flit['profane_flit'] == 'yes':
        return "profane"
    
    return jsonify({
        "flit": dict(flit)
    })


@app.route("/sitemap.xml")
def sitemap():
  return sitemapper.generate()

if __name__ == "__main__":
    app.run(debug=False)
