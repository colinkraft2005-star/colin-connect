import streamlit as st
import sqlite3
import pandas as pd
from datetime import datetime
from io import BytesIO

# ---------- CONFIG ----------
st.set_page_config(page_title="Rush Tracker", page_icon="🏠", layout="wide")

DB_PATH = "rush.db"
HOUSE_PIN = st.secrets.get("HOUSE_PIN", "changeme")        # regular members
ADMIN_PIN = st.secrets.get("ADMIN_PIN", "adminchangeme")   # the one person who can tag "dirty"

# ---------- DB SETUP ----------
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pnms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            hometown TEXT,
            major TEXT,
            photo BLOB,
            dirty INTEGER DEFAULT 0,
            checked_in_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pnm_id INTEGER,
            member_name TEXT,
            vote INTEGER,
            UNIQUE(pnm_id, member_name)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pnm_id INTEGER,
            member_name TEXT,
            comment TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    return conn

conn = get_conn()

# ---------- HELPERS ----------
def fetch_pnms(dirty=None):
    df = pd.read_sql_query("SELECT * FROM pnms ORDER BY checked_in_at DESC", conn)
    if dirty is not None:
        df = df[df["dirty"] == (1 if dirty else 0)]
    return df

def vote_counts(pnm_id):
    row = conn.execute(
        "SELECT "
        "COALESCE(SUM(CASE WHEN vote=1 THEN 1 ELSE 0 END),0), "
        "COALESCE(SUM(CASE WHEN vote=0 THEN 1 ELSE 0 END),0), "
        "COALESCE(SUM(CASE WHEN vote=-1 THEN 1 ELSE 0 END),0) "
        "FROM votes WHERE pnm_id=?",
        (pnm_id,)
    ).fetchone()
    return row  # (yes, maybe, no)

def my_vote(pnm_id, member_name):
    row = conn.execute(
        "SELECT vote FROM votes WHERE pnm_id=? AND member_name=?",
        (pnm_id, member_name)
    ).fetchone()
    return row[0] if row else None

def cast_vote(pnm_id, member_name, value):
    existing = my_vote(pnm_id, member_name)
    if existing == value:
        conn.execute("DELETE FROM votes WHERE pnm_id=? AND member_name=?", (pnm_id, member_name))
    else:
        conn.execute(
            "INSERT INTO votes (pnm_id, member_name, vote) VALUES (?,?,?) "
            "ON CONFLICT(pnm_id, member_name) DO UPDATE SET vote=excluded.vote",
            (pnm_id, member_name, value)
        )
    conn.commit()

def add_comment(pnm_id, member_name, text):
    conn.execute(
        "INSERT INTO comments (pnm_id, member_name, comment) VALUES (?,?,?)",
        (pnm_id, member_name, text)
    )
    conn.commit()

def get_comments(pnm_id):
    return conn.execute(
        "SELECT member_name, comment, created_at FROM comments WHERE pnm_id=? ORDER BY created_at DESC",
        (pnm_id,)
    ).fetchall()

def set_dirty(pnm_id, value):
    conn.execute("UPDATE pnms SET dirty=? WHERE id=?", (1 if value else 0, pnm_id))
    conn.commit()

def render_pnm_card(row, member_name, is_admin):
    with st.container(border=True):
        c1, c2 = st.columns([1, 3])
        with c1:
            if row["photo"] is not None:
                st.image(BytesIO(row["photo"]), width=150)
            else:
                st.write("No photo")
        with c2:
            st.subheader(row["name"])
            st.write(f"{row['hometown']} · {row['major']}")

            yes, maybe, no = vote_counts(row["id"])
            mine = my_vote(row["id"], member_name)

            vc1, vc2, vc3, vc4 = st.columns([1, 1, 1, 2])
            with vc1:
                label = f"✅ {yes}" + (" ✓" if mine == 1 else "")
                if st.button(label, key=f"yes_{row['id']}"):
                    cast_vote(row["id"], member_name, 1)
                    st.rerun()
            with vc2:
                label = f"🤔 {maybe}" + (" ✓" if mine == 0 else "")
                if st.button(label, key=f"maybe_{row['id']}"):
                    cast_vote(row["id"], member_name, 0)
                    st.rerun()
            with vc3:
                label = f"❌ {no}" + (" ✓" if mine == -1 else "")
                if st.button(label, key=f"no_{row['id']}"):
                    cast_vote(row["id"], member_name, -1)
                    st.rerun()
            with vc4:
                if is_admin:
                    if row["dirty"]:
                        if st.button("↩️ Move back", key=f"undirty_{row['id']}"):
                            set_dirty(row["id"], False)
                            st.rerun()
                    else:
                        if st.button("🚩 Dirty", key=f"dirty_{row['id']}"):
                            set_dirty(row["id"], True)
                            st.rerun()

            with st.expander(f"Comments ({len(get_comments(row['id']))})"):
                new_comment = st.text_input("Add a comment", key=f"comment_input_{row['id']}")
                if st.button("Post", key=f"post_{row['id']}"):
                    if new_comment.strip():
                        add_comment(row["id"], member_name, new_comment.strip())
                        st.rerun()
                for cmt_member, comment, created_at in get_comments(row["id"]):
                    st.write(f"**{cmt_member}**: {comment}")

# ---------- TOP-LEVEL NAV ----------
if "area" not in st.session_state:
    st.session_state.area = "checkin"

with st.sidebar:
    st.write("### Rush Tracker")
    if st.button("📸 PNM Check-In", use_container_width=True):
        st.session_state.area = "checkin"
        st.rerun()
    if st.button("🔑 Member Area", use_container_width=True):
        st.session_state.area = "member"
        st.rerun()

# ---------- PUBLIC: PNM CHECK-IN (no PIN) ----------
if st.session_state.area == "checkin":
    st.title("📸 Check In")
    st.caption("Take a photo and fill this out — takes 30 seconds.")

    with st.form("checkin_form", clear_on_submit=True):
        photo = st.camera_input("Take your photo")
        name = st.text_input("Name")
        hometown = st.text_input("Hometown")
        major = st.text_input("Major")

        submitted = st.form_submit_button("Check In", type="primary")
        if submitted:
            if not name.strip():
                st.error("Name is required.")
            else:
                photo_bytes = photo.getvalue() if photo is not None else None
                conn.execute(
                    "INSERT INTO pnms (name, hometown, major, photo, checked_in_at) VALUES (?,?,?,?,?)",
                    (name.strip(), hometown.strip(), major.strip(), photo_bytes, datetime.now())
                )
                conn.commit()
                st.success(f"Thanks {name}, you're checked in!")

# ---------- MEMBER AREA (PIN required) ----------
else:
    if "authed" not in st.session_state:
        st.session_state.authed = False
    if "member_name" not in st.session_state:
        st.session_state.member_name = ""
    if "is_admin" not in st.session_state:
        st.session_state.is_admin = False

    if not st.session_state.authed:
        st.title("🔑 Member Area")
        st.subheader("Enter the house code to continue")
        pin = st.text_input("PIN", type="password")
        name = st.text_input("Your first name (used to tag your votes/comments)")
        if st.button("Enter", type="primary"):
            if not name.strip():
                st.error("Enter your name so we can attribute your votes.")
            elif pin == ADMIN_PIN:
                st.session_state.authed = True
                st.session_state.member_name = name.strip()
                st.session_state.is_admin = True
                st.rerun()
            elif pin == HOUSE_PIN:
                st.session_state.authed = True
                st.session_state.member_name = name.strip()
                st.session_state.is_admin = False
                st.rerun()
            else:
                st.error("Wrong PIN.")
        st.stop()

    st.sidebar.write(f"Signed in as **{st.session_state.member_name}**" + (" (admin)" if st.session_state.is_admin else ""))
    if st.sidebar.button("Switch user"):
        st.session_state.authed = False
        st.session_state.member_name = ""
        st.session_state.is_admin = False
        st.rerun()

    page = st.sidebar.radio("Go to", ["Vote & Comment", "Dirty Tab", "Leaderboard / Export"])
    member_name = st.session_state.member_name
    is_admin = st.session_state.is_admin

    if page == "Vote & Comment":
        st.title("👍 Vote & Comment")
        df = fetch_pnms(dirty=False)

        search = st.text_input("Search by name")
        if search:
            df = df[df["name"].str.contains(search, case=False, na=False)]

        if df.empty:
            st.info("No PNMs checked in yet.")
        for _, row in df.iterrows():
            render_pnm_card(row, member_name, is_admin)

    elif page == "Dirty Tab":
        st.title("🚩 Dirty Rush List")
        st.caption("Guys tagged for dirty rush.")
        df = fetch_pnms(dirty=True)

        if df.empty:
            st.info("Nobody's been tagged yet.")
        for _, row in df.iterrows():
            render_pnm_card(row, member_name, is_admin)

    elif page == "Leaderboard / Export":
        st.title("📊 Leaderboard")
        df = fetch_pnms()

        if df.empty:
            st.info("No PNMs checked in yet.")
        else:
            rows = []
            for _, row in df.iterrows():
                yes, maybe, no = vote_counts(row["id"])
                rows.append({
                    "Name": row["name"],
                    "Hometown": row["hometown"],
                    "Major": row["major"],
                    "Yes": yes,
                    "Maybe": maybe,
                    "No": no,
                    "Net": yes - no,
                    "Dirty": "Yes" if row["dirty"] else "",
                    "Comments": len(get_comments(row["id"])),
                    "Checked in": row["checked_in_at"],
                })
            board = pd.DataFrame(rows).sort_values("Net", ascending=False)
            st.dataframe(board, use_container_width=True, hide_index=True)

            csv = board.to_csv(index=False).encode("utf-8")
            st.download_button("Download CSV", csv, "rush_leaderboard.csv", "text/csv")
