"""SafeLocate - consent-based live location sharing.

Privacy rules enforced by design:
  * Sharing starts only after the browser's own GPS permission AND an explicit tap.
  * Contacts are invited by email and must accept; there is NO phone-number lookup.
  * Every share has an expiry (max 24h). The owner can revoke instantly.
  * Only the latest point is stored; it is deleted the moment sharing stops.
"""
import hashlib, hmac, logging, os, re, secrets, smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage
from functools import wraps

from dotenv import load_dotenv
from flask import (Flask, flash, g, jsonify, redirect, render_template, request,
                   session, url_for)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()
env_bool = lambda k, d="false": os.getenv(k, d).lower() in ("1", "true", "yes")
IS_PROD = os.getenv("APP_ENV", "production") == "production"

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or (None if IS_PROD else "dev-only-key")
if not app.secret_key:
    raise RuntimeError("SECRET_KEY must be set in production")

db_url = os.getenv("DATABASE_URL", "")
if db_url.startswith("mysql://"):
    db_url = db_url.replace("mysql://", "mysql+pymysql://", 1)
engine_opts = {"pool_pre_ping": True, "pool_recycle": 280}
if os.getenv("DB_SSL_CA"):
    engine_opts["connect_args"] = {"ssl": {"ca": os.getenv("DB_SSL_CA")}}
app.config.update(
    SQLALCHEMY_DATABASE_URI=db_url or "sqlite:///dev.db",
    SQLALCHEMY_ENGINE_OPTIONS=engine_opts if db_url else {},
    SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=IS_PROD, PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    MAX_CONTENT_LENGTH=16 * 1024,
)
if env_bool("TRUST_PROXY", "true"):
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

db = SQLAlchemy(app)
csrf = CSRFProtect(app)
limiter = Limiter(get_remote_address, app=app, default_limits=["300 per hour"],
                  storage_uri="memory://")  # use Redis if you scale past one instance
logging.basicConfig(level=logging.INFO)

DURATIONS = {15, 60, 240, 480, 1440}  # minutes; 24h hard cap
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
now = datetime.utcnow
iso = lambda d: d.isoformat() + "Z" if d else None


# ----------------------------------------------------------------- models
class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    email = db.Column(db.String(190), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    email_verified = db.Column(db.Boolean, default=False, nullable=False)
    sharing_on = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=now, nullable=False)


class Otp(db.Model):
    __tablename__ = "otps"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    purpose = db.Column(db.String(12), nullable=False)  # register | login
    code_hash = db.Column(db.String(64), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    attempts = db.Column(db.Integer, default=0, nullable=False)
    used = db.Column(db.Boolean, default=False, nullable=False)


class Share(db.Model):
    __tablename__ = "shares"
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    contact_email = db.Column(db.String(190), nullable=False, index=True)
    status = db.Column(db.String(12), default="pending", nullable=False)
    duration_min = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=now, nullable=False)
    invite_expires_at = db.Column(db.DateTime, nullable=False)
    expires_at = db.Column(db.DateTime)


class Location(db.Model):
    __tablename__ = "locations"
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    lat = db.Column(db.Double, nullable=False)
    lng = db.Column(db.Double, nullable=False)
    accuracy = db.Column(db.Double)
    updated_at = db.Column(db.DateTime, nullable=False)


# ---------------------------------------------------------------- helpers
def send_mail(to, subject, body):
    if env_bool("MAIL_DEV_MODE"):
        app.logger.warning("DEV MAIL to=%s subject=%s\n%s", to, subject, body)
        return
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = os.getenv("MAIL_FROM"), to, subject
    msg.set_content(body)
    with smtplib.SMTP(os.getenv("SMTP_HOST"), int(os.getenv("SMTP_PORT", 587)), timeout=15) as s:
        s.starttls()
        s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD"))
        s.send_message(msg)


def hash_code(uid, code):
    return hmac.new(app.secret_key.encode(), f"{uid}:{code}".encode(), hashlib.sha256).hexdigest()


def issue_otp(user, purpose):
    Otp.query.filter_by(user_id=user.id, used=False).update({"used": True})
    code = f"{secrets.randbelow(10**6):06d}"
    db.session.add(Otp(user_id=user.id, purpose=purpose, code_hash=hash_code(user.id, code),
                       expires_at=now() + timedelta(minutes=10)))
    db.session.commit()
    send_mail(user.email, "Your SafeLocate verification code",
              f"Your code is {code}. It expires in 10 minutes.\n"
              "If you didn't request it, ignore this email - nobody can access your account without it.")


def check_otp(user, purpose, code):
    o = (Otp.query.filter_by(user_id=user.id, purpose=purpose, used=False)
         .order_by(Otp.id.desc()).first())
    if not o or o.expires_at < now() or o.attempts >= 5:
        return False
    o.attempts += 1
    ok = hmac.compare_digest(o.code_hash, hash_code(user.id, code.strip()))
    if ok:
        o.used = True
    db.session.commit()
    return ok


def login_user(user):
    session.clear()
    session.permanent = True
    session["uid"] = user.id


@app.before_request
def load_user():
    g.user = db.session.get(User, session["uid"]) if "uid" in session else None


def login_required(api=False):
    def deco(f):
        @wraps(f)
        def wrapper(*a, **kw):
            if not g.user:
                return (jsonify(error="Please sign in again."), 401) if api else redirect(url_for("login"))
            return f(*a, **kw)
        return wrapper
    return deco


@app.after_request
def security_headers(r):
    r.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' https://unpkg.com; "
        "style-src 'self' 'unsafe-inline' https://unpkg.com; "
        "img-src 'self' data: https://*.tile.openstreetmap.org https://unpkg.com; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["X-Frame-Options"] = "DENY"
    r.headers["Referrer-Policy"] = "same-origin"
    r.headers["Permissions-Policy"] = "geolocation=(self), camera=(), microphone=()"
    if IS_PROD:
        r.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.path.startswith("/api/"):
        r.headers["Cache-Control"] = "no-store"
    return r


# ------------------------------------------------------------- auth pages
@app.get("/")
def index():
    return redirect(url_for("dashboard" if g.user else "login"))


@app.get("/healthz")
@limiter.exempt
def healthz():
    return "ok"


@app.route("/register", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()[:80]
        email = request.form.get("email", "").strip().lower()
        pw, pw2 = request.form.get("password", ""), request.form.get("confirm", "")
        if not name or not EMAIL_RE.match(email):
            flash("Enter your name and a valid email address.", "error")
        elif len(pw) < 10:
            flash("Use a password of at least 10 characters.", "error")
        elif pw != pw2:
            flash("The passwords don't match.", "error")
        else:
            user = User.query.filter_by(email=email).first()
            if user and user.email_verified:  # don't reveal that the account exists
                flash("If this email can be registered, a code is on its way. Otherwise, sign in.", "info")
                return redirect(url_for("login"))
            if not user:
                user = User(name=name, email=email, password_hash="")
                db.session.add(user)
            user.name, user.password_hash = name, generate_password_hash(pw)
            db.session.commit()
            return start_otp(user, "register")
    return render_template("register.html")


def start_otp(user, purpose):
    try:
        issue_otp(user, purpose)
    except Exception:
        app.logger.exception("OTP email failed")
        flash("We couldn't send the code. Try again in a moment.", "error")
        return redirect(url_for("login"))
    session.clear()
    session["pending_uid"], session["pending_purpose"] = user.id, purpose
    return redirect(url_for("verify"))


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("8 per minute", methods=["POST"])
def login():
    if g.user:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = User.query.filter_by(email=email).first()
        ok = check_password_hash(user.password_hash if user else generate_password_hash("x"),
                                 request.form.get("password", ""))
        if user and ok:
            return start_otp(user, "login" if user.email_verified else "register")
        flash("Email or password is incorrect.", "error")
    return render_template("login.html")


@app.route("/verify", methods=["GET", "POST"])
@limiter.limit("10 per 10 minutes", methods=["POST"])
def verify():
    user = db.session.get(User, session.get("pending_uid") or 0)
    purpose = session.get("pending_purpose")
    if not user:
        return redirect(url_for("login"))
    if request.method == "POST":
        if check_otp(user, purpose, request.form.get("code", "")):
            user.email_verified = True
            db.session.commit()
            login_user(user)
            return redirect(url_for("dashboard"))
        flash("That code is wrong or has expired.", "error")
    return render_template("verify.html", email=user.email)


@app.post("/verify/resend")
@limiter.limit("3 per 10 minutes")
def resend():
    user = db.session.get(User, session.get("pending_uid") or 0)
    if not user:
        return redirect(url_for("login"))
    uid, purpose = user.id, session["pending_purpose"]
    try:
        issue_otp(user, purpose)
        flash("A new code was sent.", "info")
    except Exception:
        flash("We couldn't send the code. Try again shortly.", "error")
    session["pending_uid"], session["pending_purpose"] = uid, purpose
    return redirect(url_for("verify"))


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/dashboard")
@login_required()
def dashboard():
    return render_template("dashboard.html")


# -------------------------------------------------------------------- API
def sweep(user):
    """Lazily expire shares so expired access stops immediately."""
    t = now()
    q = Share.query.filter(db.or_(Share.owner_id == user.id, Share.contact_email == user.email))
    for s in q.filter(Share.status.in_(("pending", "active"))):
        if (s.status == "active" and s.expires_at <= t) or (s.status == "pending" and s.invite_expires_at <= t):
            s.status = "expired"
    db.session.commit()


@app.get("/api/state")
@login_required(api=True)
def state():
    u = g.user
    sweep(u)
    outgoing = [dict(id=s.id, email=s.contact_email, status=s.status, expires_at=iso(s.expires_at),
                     duration=s.duration_min)
                for s in Share.query.filter_by(owner_id=u.id).filter(Share.status.in_(("pending", "active")))]
    incoming = []
    for s in Share.query.filter_by(contact_email=u.email).filter(Share.status.in_(("pending", "active"))):
        owner = db.session.get(User, s.owner_id)
        item = dict(id=s.id, owner=owner.name, status=s.status, expires_at=iso(s.expires_at),
                    duration=s.duration_min, sharing=bool(owner.sharing_on))
        loc = db.session.get(Location, owner.id) if s.status == "active" and owner.sharing_on else None
        if loc:
            item["location"] = dict(lat=loc.lat, lng=loc.lng, accuracy=loc.accuracy, updated_at=iso(loc.updated_at))
        incoming.append(item)
    return jsonify(me=dict(name=u.name, email=u.email, sharing_on=u.sharing_on),
                   outgoing=outgoing, incoming=incoming, server_time=iso(now()))


@app.post("/api/invite")
@login_required(api=True)
@limiter.limit("10 per hour")
def invite():
    d = request.get_json(silent=True) or {}
    email, minutes = str(d.get("email", "")).strip().lower(), d.get("minutes")
    if not EMAIL_RE.match(email) or minutes not in DURATIONS:
        return jsonify(error="Enter a valid email and choose a duration."), 400
    if email == g.user.email:
        return jsonify(error="You can't invite yourself."), 400
    if Share.query.filter_by(owner_id=g.user.id, contact_email=email).filter(
            Share.status.in_(("pending", "active"))).first():
        return jsonify(error="That contact already has an active invite."), 409
    db.session.add(Share(owner_id=g.user.id, contact_email=email, duration_min=minutes,
                         invite_expires_at=now() + timedelta(hours=48)))
    db.session.commit()
    try:  # same response whether or not the address has an account
        send_mail(email, f"{g.user.name} invited you on SafeLocate",
                  f"{g.user.name} would like to share their live location with you.\n"
                  "Sign in to SafeLocate to accept or decline. If you don't know them, ignore this message.")
    except Exception:
        app.logger.exception("invite email failed")
    return jsonify(ok=True)


def own_share(sid, owner=True):
    s = db.session.get(Share, sid)
    if not s:
        return None
    if owner and s.owner_id == g.user.id:
        return s
    if not owner and s.contact_email == g.user.email:
        return s
    return None


@app.post("/api/share/<int:sid>/respond")
@login_required(api=True)
def respond(sid):
    s = own_share(sid, owner=False)
    accept = bool((request.get_json(silent=True) or {}).get("accept"))
    if not s or s.status != "pending" or s.invite_expires_at <= now():
        return jsonify(error="This invite is no longer available."), 404
    s.status = "active" if accept else "declined"
    if accept:
        s.expires_at = now() + timedelta(minutes=s.duration_min)
    db.session.commit()
    return jsonify(ok=True)


@app.post("/api/share/<int:sid>/revoke")
@login_required(api=True)
def revoke(sid):
    s = own_share(sid) or own_share(sid, owner=False)  # owner revokes; contact may stop viewing
    if not s:
        return jsonify(error="Not found."), 404
    s.status = "revoked"
    db.session.commit()
    return jsonify(ok=True)


@app.post("/api/share/<int:sid>/extend")
@login_required(api=True)
def extend(sid):
    s = own_share(sid)
    minutes = (request.get_json(silent=True) or {}).get("minutes")
    if not s or s.status != "active" or minutes not in DURATIONS:
        return jsonify(error="Can't extend this share."), 400
    s.expires_at = now() + timedelta(minutes=minutes)  # always measured from now: max 24h ahead
    db.session.commit()
    return jsonify(ok=True)


@app.post("/api/sharing/start")
@login_required(api=True)
def sharing_start():
    g.user.sharing_on = True
    db.session.commit()
    return jsonify(ok=True)


def wipe_location(uid):
    Location.query.filter_by(owner_id=uid).delete()


@app.post("/api/sharing/stop")
@login_required(api=True)
def sharing_stop():
    g.user.sharing_on = False
    wipe_location(g.user.id)
    db.session.commit()
    return jsonify(ok=True)


@app.post("/api/stop-all")
@login_required(api=True)
def stop_all():
    """Panic button: stop broadcasting, delete the stored point, revoke every share."""
    g.user.sharing_on = False
    wipe_location(g.user.id)
    Share.query.filter_by(owner_id=g.user.id).filter(Share.status.in_(("pending", "active"))).update(
        {"status": "revoked"}, synchronize_session=False)
    db.session.commit()
    return jsonify(ok=True)


@app.post("/api/location")
@login_required(api=True)
@limiter.limit("20 per minute")
def update_location():
    if not g.user.sharing_on:
        return jsonify(error="Sharing is off."), 409
    d = request.get_json(silent=True) or {}
    try:
        lat, lng = float(d["lat"]), float(d["lng"])
        acc = float(d["accuracy"]) if d.get("accuracy") is not None else None
    except (KeyError, TypeError, ValueError):
        return jsonify(error="Invalid location."), 400
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return jsonify(error="Invalid location."), 400
    loc = db.session.get(Location, g.user.id) or Location(owner_id=g.user.id)
    loc.lat, loc.lng, loc.accuracy, loc.updated_at = lat, lng, acc, now()
    db.session.add(loc)
    db.session.commit()
    return jsonify(ok=True)


@app.errorhandler(429)
def too_many(e):
    if request.path.startswith("/api/"):
        return jsonify(error="Too many requests. Slow down."), 429
    flash("Too many attempts. Please wait a few minutes.", "error")
    return render_template("login.html"), 429


# -------------------------------------------------------------------- CLI
@app.cli.command("init-db")
def init_db():
    db.create_all()
    print("Database ready.")


@app.cli.command("purge")
def purge():
    """Run daily (cron): delete old OTPs, dead shares, and orphaned locations."""
    cut = now() - timedelta(days=7)
    Otp.query.filter(Otp.expires_at < now() - timedelta(hours=1)).delete()
    Share.query.filter(Share.status.in_(("declined", "revoked", "expired")), Share.created_at < cut).delete()
    for loc in Location.query.join(User, User.id == Location.owner_id).filter(User.sharing_on.is_(False)):
        db.session.delete(loc)
    db.session.commit()
    print("Purged.")


if __name__ == "__main__":
    app.run(debug=not IS_PROD)
