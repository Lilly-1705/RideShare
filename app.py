from flask import Flask, request, jsonify
from flask_cors import CORS
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, date, time as dtime, timedelta
import threading
import time
import traceback

from models import get_db
from config import EMAIL_CONFIG

app = Flask(__name__)
CORS(app)

# ------------------ EMAIL ------------------
def send_email(to_email: str, subject: str, body: str):
    """Send email via Gmail SMTP with error handling."""
    sender_email = EMAIL_CONFIG["SENDER_EMAIL"]
    sender_password = EMAIL_CONFIG["SENDER_PASSWORD"]

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = sender_email
    msg["To"] = to_email

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, to_email, msg.as_string())
        print(f"✅ Email sent to {to_email}")
        return True
    except Exception as e:
        print(f"❌ Email send failed to {to_email}:", e)
        return False


# ------------------ DB HELPERS ------------------
def fetch_all_pending_rides():
    """Fetch all rides not yet in groups and travel_date >= today."""
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT r.* FROM rides r
        LEFT JOIN group_members gm ON r.id = gm.ride_id
        WHERE gm.ride_id IS NULL 
        AND r.travel_date >= CAST(GETDATE() AS DATE)
    """)
    columns = [c[0] for c in cur.description]
    rows = cur.fetchall()
    db.close()
    return [dict(zip(columns, row)) for row in rows]


def ride_in_any_group(ride_id):
    """Return True if ride_id appears in group_members."""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT 1 FROM group_members WHERE ride_id = ?", (ride_id,))
    found = cur.fetchone() is not None
    db.close()
    return found


def get_group_info(group_id):
    """Get group details including current member count."""
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT g.group_id, g.leader_id, g.max_size,
               COUNT(gm.ride_id) as member_count
        FROM ride_groups g
        LEFT JOIN group_members gm ON g.group_id = gm.group_id
        WHERE g.group_id = ?
        GROUP BY g.group_id, g.leader_id, g.max_size
    """, (group_id,))
    row = cur.fetchone()
    db.close()
    if row:
        return {
            "group_id": row[0],
            "leader_id": row[1],
            "max_size": row[2],
            "member_count": row[3]
        }
    return None


def get_all_non_full_groups():
    """Get all groups that aren't full yet."""
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT g.group_id, g.leader_id, g.max_size,
               COUNT(gm.ride_id) as member_count
        FROM ride_groups g
        LEFT JOIN group_members gm ON g.group_id = gm.group_id
        GROUP BY g.group_id, g.leader_id, g.max_size
        HAVING COUNT(gm.ride_id) < g.max_size
    """)
    rows = cur.fetchall()
    db.close()
    return [{
        "group_id": r[0],
        "leader_id": r[1],
        "max_size": r[2],
        "member_count": r[3]
    } for r in rows]


def get_ride_details(ride_id):
    """Get ride details by ID from rides table or backup."""
    db = get_db()
    cur = db.cursor()
    
    # Try rides table first
    cur.execute("SELECT * FROM rides WHERE id = ?", (ride_id,))
    row = cur.fetchone()
    
    if row:
        columns = [c[0] for c in cur.description]
        db.close()
        return dict(zip(columns, row))
    
    # Try backup table
    cur.execute("SELECT * FROM rides_backup WHERE id = ?", (ride_id,))
    row = cur.fetchone()
    
    if row:
        columns = [c[0] for c in cur.description]
        db.close()
        return dict(zip(columns, row))
    
    db.close()
    return None

def get_group_members_details(group_id):
    """Get all member details for a group."""
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT gm.ride_id
        FROM group_members gm
        WHERE gm.group_id = ?
    """, (group_id,))
    ride_ids = [row[0] for row in cur.fetchall()]
    db.close()
    
    members = []
    for rid in ride_ids:
        details = get_ride_details(rid)
        if details:
            members.append(details)
    return members


def create_group_db(leader_id, max_people):
    """Create a new row in ride_groups and return the new group_id."""
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        INSERT INTO ride_groups (leader_id, max_size, created_at)
        OUTPUT INSERTED.group_id
        VALUES (?, ?, GETDATE())
    """, (leader_id, max_people))
    group_id = cur.fetchone()[0]
    db.commit()
    db.close()
    return group_id


def backup_ride(ride_details):
    """Backup ride details before deletion. Skip if already backed up."""
    db = get_db()
    cur = db.cursor()
    try:
        # Check if already backed up
        cur.execute("SELECT 1 FROM rides_backup WHERE id = ?", (ride_details['id'],))
        if cur.fetchone():
            print(f"ℹ️ Ride {ride_details['id']} already backed up, skipping")
            db.close()
            return
        
        # Insert backup
        cur.execute("""
            INSERT INTO rides_backup 
            (id, name, gender, reg_no, phone, vit_mail, travel_date, start_time, end_time,
             from_location, to_location, max_people, share_opposite_gender, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, GETDATE())
        """, (
            ride_details['id'], ride_details['name'], ride_details['gender'],
            ride_details['reg_no'], ride_details['phone'], ride_details['vit_mail'],
            ride_details['travel_date'], ride_details['start_time'], ride_details['end_time'],
            ride_details['from_location'], ride_details['to_location'],
            ride_details['max_people'], ride_details['share_opposite_gender']
        ))
        db.commit()
        print(f"✅ Ride {ride_details['id']} backed up successfully")
    except Exception as e:
        print(f"⚠️ Backup failed for ride {ride_details['id']}: {e}")
    finally:
        db.close()


def add_group_member_db(group_id, ride_id, ride_details):
    """Add a ride to group_members, backup, then delete from rides."""
    # Backup first
    backup_ride(ride_details)
    
    db = get_db()
    cur = db.cursor()
    # Insert into group_members
    cur.execute("INSERT INTO group_members (group_id, ride_id) VALUES (?, ?)", (group_id, ride_id))
    # Delete from rides table
    cur.execute("DELETE FROM rides WHERE id = ?", (ride_id,))
    db.commit()
    db.close()


# ------------------ UTIL ------------------
def parse_datetime_from_ride(ride):
    """Normalize ride's travel_date and start_time into datetime."""
    travel_date = ride["travel_date"]
    if isinstance(travel_date, str):
        travel_date = datetime.strptime(travel_date, "%Y-%m-%d").date()
    elif isinstance(travel_date, datetime):
        travel_date = travel_date.date()

    start_time = ride["start_time"]
    if isinstance(start_time, str):
        if len(start_time) == 5:
            start_time = datetime.strptime(start_time + ":00", "%H:%M:%S").time()
        else:
            start_time = datetime.strptime(start_time, "%H:%M:%S").time()
    elif isinstance(start_time, dtime):
        pass

    return datetime.combine(travel_date, start_time)


def times_overlap(a_start, a_end, b_start, b_end):
    """Check if two time intervals overlap."""
    def to_time(t):
        if isinstance(t, dtime):
            return t
        if isinstance(t, str):
            if len(t) == 5:
                return datetime.strptime(t + ":00", "%H:%M:%S").time()
            else:
                return datetime.strptime(t, "%H:%M:%S").time()
        return t

    a_s = to_time(a_start)
    a_e = to_time(a_end)
    b_s = to_time(b_start)
    b_e = to_time(b_end)

    return not (a_e <= b_s or a_s >= b_e)


# ------------------ MATCHING LOGIC ------------------
def can_match(ride, other):
    """Check if two rides can be matched based on constraints."""
    if ride["id"] == other["id"]:
        return False

    # Same date
    if str(ride["travel_date"]) != str(other["travel_date"]):
        return False

    # Same locations
    if ride["from_location"] != other["from_location"] or ride["to_location"] != other["to_location"]:
        return False

    # Time overlap
    if not times_overlap(ride["start_time"], ride["end_time"], other["start_time"], other["end_time"]):
        return False

    # Gender rules
    if ride["gender"] == other["gender"]:
        return True

    # Opposite genders: both must allow
    ride_share = int(ride.get("share_opposite_gender", 0))
    other_share = int(other.get("share_opposite_gender", 0))
    return ride_share == 1 and other_share == 1


def build_partial_group_for_leader(leader, pool):
    """
    Build a group starting with leader, adding compatible rides.
    NEW BEHAVIOR: Can create groups with 2+ members (doesn't need to reach max_size immediately)
    Returns group if at least 2 members found, None otherwise.
    """
    group = [leader]
    min_max_people = int(leader["max_people"])
    
    for other in pool:
        if other["id"] == leader["id"]:
            continue
        if not can_match(leader, other):
            continue
        
        group.append(other)
        # Update min_max_people (this determines final group capacity)
        min_max_people = min(min_max_people, int(other["max_people"]))
        
        # Stop if we've reached the capacity
        if len(group) == min_max_people:
            return group, min_max_people
    
    # Return partial group if we have at least 2 members
    if len(group) >= 2:
        return group, min_max_people
    
    return None, 0


# ------------------ GROUP CREATION & NOTIFICATIONS ------------------
def create_partial_group_and_notify(members, max_size):
    """
    Create a new group with given members (can be partial) and send notifications.
    NEW: Sends different emails based on whether group is full or not.
    """
    leader = sorted(members, key=lambda r: r["id"])[0]

    # Create group with the determined max_size
    group_id = create_group_db(leader["id"], max_size)
    print(f"✅ Created group {group_id} with leader {leader['id']} (max_size: {max_size}, current: {len(members)})")

    # Add all members
    for m in members:
        add_group_member_db(group_id, m["id"], m)

    # Check if group is full
    is_full = (len(members) == max_size)

    # Prepare member details
    details = "\n\n".join([
        f"Name: {r['name']}\nPhone: {r['phone']}\nEmail: {r['vit_mail']}"
        for r in members
    ])

    # Send emails based on group status
    if is_full:
        # Group is complete
        for r in members:
            body = (
                f"Hey {r['name']},\n\n"
                "🎉 Your ride group has been successfully created and is now FULL!\n\n"
                f"Leader: {leader['name']}\n\n"
                f"Group Members ({len(members)}/{max_size}):\n{details}\n\n"
                "Please coordinate with your group members for the ride.\n"
                "Happy Ride Sharing! 🚗💚"
            )
            send_email(r["vit_mail"], "🎉 Ride Group FULL!", body)
    else:
        # Group is partial - waiting for more members
        for r in members:
            body = (
                f"Hey {r['name']},\n\n"
                "✅ Your ride group has been created!\n\n"
                f"Leader: {leader['name']}\n\n"
                f"Current Members ({len(members)}/{max_size}):\n{details}\n\n"
                f"Your group can accommodate up to {max_size} people total.\n"
                "We'll notify you when new members join.\n\n"
                "Happy Ride Sharing! 🚗💚"
            )
            send_email(r["vit_mail"], "✅ Ride Group Created (Waiting for More)", body)


def add_member_to_group_and_notify(group_id, new_member):
    """Add a new member to an existing group and notify everyone."""
    # Get existing members before adding
    existing_members = get_group_members_details(group_id)
    
    # Add new member
    add_group_member_db(group_id, new_member["id"], new_member)
    
    # Check if group is now full
    group_info = get_group_info(group_id)
    is_full = group_info["member_count"] >= group_info["max_size"]
    
    # Notify existing members about new member
    new_info = f"Name: {new_member['name']}\nPhone: {new_member['phone']}\nEmail: {new_member['vit_mail']}"
    
    for m in existing_members:
        if is_full:
            all_members = existing_members + [new_member]
            all_details = "\n\n".join([
                f"Name: {r['name']}\nPhone: {r['phone']}\nEmail: {r['vit_mail']}"
                for r in all_members
            ])
            body = (
                f"Hey {m['name']},\n\n"
                "🎉 Your ride group is now COMPLETE!\n\n"
                f"New member joined:\n{new_info}\n\n"
                f"All Members ({group_info['member_count']}/{group_info['max_size']}):\n{all_details}\n\n"
                "Please coordinate with your group members for the ride.\n"
                "Happy Ride Sharing! 🚗💚"
            )
            send_email(m["vit_mail"], "🎉 Ride Group NOW FULL!", body)
        else:
            body = (
                f"Hey {m['name']},\n\n"
                "✅ A new member has joined your ride group!\n\n"
                f"New member:\n{new_info}\n\n"
                f"Group status: {group_info['member_count']}/{group_info['max_size']} members\n\n"
                "We'll notify you when the group is complete.\n"
                "Happy Ride Sharing! 🚗💚"
            )
            send_email(m["vit_mail"], "✅ New Member Joined Your Group", body)
    
    # Notify new member
    members_text = "\n\n".join([
        f"Name: {m['name']}\nPhone: {m['phone']}\nEmail: {m['vit_mail']}"
        for m in existing_members
    ])
    
    if is_full:
        all_members = existing_members + [new_member]
        all_details = "\n\n".join([
            f"Name: {r['name']}\nPhone: {r['phone']}\nEmail: {r['vit_mail']}"
            for r in all_members
        ])
        body_new = (
            f"Hey {new_member['name']},\n\n"
            "🎉 You've been added to a ride group and it's now COMPLETE!\n\n"
            f"All Members ({group_info['member_count']}/{group_info['max_size']}):\n{all_details}\n\n"
            "Please coordinate with your group members for the ride.\n"
            "Happy Ride Sharing! 🚗💚"
        )
        send_email(new_member["vit_mail"], "🎉 Joined COMPLETE Ride Group!", body_new)
    else:
        body_new = (
            f"Hey {new_member['name']},\n\n"
            "✅ You've been added to an existing ride group!\n\n"
            f"Current members:\n{members_text}\n\n"
            f"Group status: {group_info['member_count']}/{group_info['max_size']} members\n\n"
            "We'll notify you when more members join. 🚗💚"
        )
        send_email(new_member["vit_mail"], "✅ You Joined a Ride Group!", body_new)


def mark_no_match_sent(ride_id):
    """Mark that no-match email has been sent for this ride."""
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("""
            IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='no_match_notified' AND xtype='U')
            CREATE TABLE no_match_notified (ride_id INT PRIMARY KEY, notified_at DATETIME DEFAULT GETDATE())
        """)
        db.commit()
        cur.execute("INSERT INTO no_match_notified (ride_id) VALUES (?)", (ride_id,))
        db.commit()
    except:
        pass
    finally:
        db.close()


def is_no_match_sent(ride_id):
    """Check if no-match email was already sent."""
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("SELECT 1 FROM no_match_notified WHERE ride_id = ?", (ride_id,))
        result = cur.fetchone() is not None
        db.close()
        return result
    except:
        db.close()
        return False


# ------------------ BACKGROUND MATCHER ------------------
def background_matcher():
    """Background thread that periodically matches rides."""
    print("🔄 Background matcher started")
    
    while True:
        try:
            # Fetch pending rides
            all_rides = fetch_all_pending_rides()
            
            if not all_rides:
                time.sleep(30)
                continue
            
            # Filter out past rides
            now = datetime.now()
            pending = []
            for r in all_rides:
                try:
                    travel_dt = parse_datetime_from_ride(r)
                    if travel_dt > now:
                        pending.append(r)
                except Exception as e:
                    print(f"⚠️ Error parsing ride {r.get('id')}: {e}")
            
            if not pending:
                time.sleep(30)
                continue
            
            # Sort by ID (earliest registration first)
            pending.sort(key=lambda x: x["id"])
            
            print(f"🔍 Checking {len(pending)} pending rides...")
            
            # PASS 1: Fill existing non-full groups
            non_full_groups = get_all_non_full_groups()
            
            for group in non_full_groups:
                leader_details = get_ride_details(group["leader_id"])
                if not leader_details:
                    continue
                
                # Find compatible pending rides
                for ride in pending[:]:  # Use slice to allow removal during iteration
                    if can_match(leader_details, ride):
                        print(f"✅ Adding ride {ride['id']} to group {group['group_id']}")
                        add_member_to_group_and_notify(group["group_id"], ride)
                        pending.remove(ride)
                        
                        # Check if group is now full
                        updated_group = get_group_info(group["group_id"])
                        if updated_group["member_count"] >= updated_group["max_size"]:
                            break
            
            # PASS 2: Create new groups from remaining pending rides (CAN BE PARTIAL)
            used_ids = set()
            
            for ride in pending:
                if ride["id"] in used_ids:
                    continue
                
                # Find compatible rides
                pool = [r for r in pending if r["id"] != ride["id"] and r["id"] not in used_ids]
                compatible_pool = [r for r in pool if can_match(ride, r)]
                compatible_pool.sort(key=lambda x: x["id"])
                
                # Try to build a group (can be partial now)
                result = build_partial_group_for_leader(ride, compatible_pool)
                
                if result[0] is not None:
                    group, max_size = result
                    print(f"✅ Creating new group with {len(group)} members (max: {max_size})")
                    create_partial_group_and_notify(group, max_size)
                    for g in group:
                        used_ids.add(g["id"])
                else:
                    # No match found - send email once
                    if not is_no_match_sent(ride["id"]):
                        body = (
                            f"Hey {ride['name']},\n\n"
                            "❌ No ride matches found yet.\n\n"
                            "We'll keep checking automatically until your travel time.\n"
                            "You'll receive an email when a match is found.\n\n"
                            "Happy Ride Sharing! 🚗"
                        )
                        if send_email(ride["vit_mail"], "No Ride Match Yet ❌", body):
                            mark_no_match_sent(ride["id"])
            
            # Sleep before next check
            time.sleep(30)
            
        except Exception as e:
            print(f"❌ Background matcher error: {e}")
            traceback.print_exc()
            time.sleep(30)


# ------------------ REGISTER ENDPOINT ------------------
@app.route("/register", methods=["POST"])
def register():
    """Handle ride registration."""
    data = request.get_json()
    if not data:
        return jsonify({"message": "Invalid JSON"}), 400

    try:
        db = get_db()
        cur = db.cursor()
        cur.execute("""
            INSERT INTO rides
            (name, gender, reg_no, phone, vit_mail, travel_date, start_time, end_time,
             from_location, to_location, max_people, share_opposite_gender, created_at)
            OUTPUT INSERTED.id
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, GETDATE())
        """, (
            data["name"], data["gender"], data["reg_no"], data["phone"], data["vit_mail"],
            data["travel_date"], data["start_time"], data["end_time"],
            data["from_location"], data["to_location"],
            int(data["max_people"]),
            1 if data["share_opposite_gender"].lower() == "yes" else 0
        ))
        ride_id = cur.fetchone()[0]
        db.commit()
        db.close()

        # Send confirmation email
        send_email(
            data["vit_mail"],
            "Registration Successful ✅",
            f"Hey {data['name']},\n\n"
            "You have been registered for ride sharing.\n"
            "We'll notify you when a match is found!\n\n"
            "Travel Details:\n"
            f"Date: {data['travel_date']}\n"
            f"Time: {data['start_time']} - {data['end_time']}\n"
            f"From: {data['from_location']}\n"
            f"To: {data['to_location']}\n\n"
            "Happy Ride Sharing! 🚗💚"
        )

        return jsonify({"message": "Registered successfully!", "ride_id": ride_id}), 200

    except Exception as e:
        print(f"❌ Registration error: {e}")
        traceback.print_exc()
        return jsonify({"message": f"Registration failed: {str(e)}"}), 500


# ------------------ RUN ------------------
if __name__ == "__main__":
    # Start background matcher thread
    matcher_thread = threading.Thread(target=background_matcher, daemon=True)
    matcher_thread.start()
    
    print("🚀 Flask server starting...")
    app.run(debug=True, host='0.0.0.0', port=5000)