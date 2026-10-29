import sqlite3
from datetime import datetime
from flask import (
    Flask,
    flash,
    g,
    redirect,
    render_template_string,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = "super_secret_automotive_key_change_in_prod"
DATABASE = "fuel_tracker.db"


# ==========================================
# 1. DATABASE & RELATIONAL SCHEMA LAYER
# ==========================================
def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
    return db


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()


def init_db():
    with app.app_context():
        db = get_db()
        cursor = db.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS fuel_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                entry_date TEXT NOT NULL,
                fuel_type TEXT NOT NULL DEFAULT 'Diesel',
                route_type TEXT NOT NULL DEFAULT 'Balanced Mix',
                traffic_level TEXT NOT NULL DEFAULT 'Moderate',
                odometer REAL NOT NULL,
                fuel_liters REAL NOT NULL,
                price_per_liter REAL NOT NULL DEFAULT 0.0,
                total_cost REAL NOT NULL,
                is_full_tank INTEGER NOT NULL DEFAULT 1,
                notes TEXT,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            )
        """)
        # Safe auto-migration for existing databases
        existing_cols = [
            row["name"]
            for row in cursor.execute("PRAGMA table_info(fuel_logs)").fetchall()
        ]
        if "fuel_type" not in existing_cols:
            cursor.execute(
                "ALTER TABLE fuel_logs ADD COLUMN fuel_type TEXT NOT NULL DEFAULT 'Diesel'"
            )
        if "price_per_liter" not in existing_cols:
            cursor.execute(
                "ALTER TABLE fuel_logs ADD COLUMN price_per_liter REAL NOT NULL DEFAULT 0.0"
            )
        if "route_type" not in existing_cols:
            cursor.execute(
                "ALTER TABLE fuel_logs ADD COLUMN route_type TEXT NOT NULL DEFAULT 'Balanced Mix'"
            )
        if "traffic_level" not in existing_cols:
            cursor.execute(
                "ALTER TABLE fuel_logs ADD COLUMN traffic_level TEXT NOT NULL DEFAULT 'Moderate'"
            )
        db.commit()


# ==========================================
# 2. OOP ANALYTICS & DRIVER SCORING ENGINE
# ==========================================
class MileageAnalyzer:
    """Calculates tank-to-tank mileage, buffers partial fill-ups,
    and evaluates driver efficiency relative to route & traffic conditions.
    """

    ROUTE_MULTIPLIERS = {
        "100% Highway": 1.22,
        "Mostly Highway": 1.12,
        "Balanced Mix": 1.00,
        "Mostly City": 0.88,
        "100% City": 0.78,
    }

    TRAFFIC_MULTIPLIERS = {
        "Free Flow": 1.08,
        "Light": 1.03,
        "Moderate": 1.00,
        "Heavy": 0.86,
        "Gridlock": 0.74,
    }

    def __init__(self, logs):
        self.logs = sorted(logs, key=lambda x: x["odometer"])

    def evaluate_driving(self, actual_kmpl, fuel_type, route_type, traffic_level):
        """Compares actual mileage against a condition-adjusted baseline
        so city gridlock driving is judged fairly compared to open highway cruising.
        """
        base_kmpl = 14.5 if fuel_type == "Diesel" else 12.5
        r_mult = self.ROUTE_MULTIPLIERS.get(route_type, 1.0)
        t_mult = self.TRAFFIC_MULTIPLIERS.get(traffic_level, 1.0)

        expected_kmpl = round(base_kmpl * r_mult * t_mult, 2)
        ratio = actual_kmpl / expected_kmpl if expected_kmpl > 0 else 1.0
        score = int(max(40, min(99, round(78 * ratio))))

        if ratio >= 1.12:
            return {
                "label": "Hypermiler",
                "style": "bg-emerald-900/70 text-emerald-300 border-emerald-600",
                "score": score,
                "expected": expected_kmpl,
            }
        elif ratio >= 0.98:
            return {
                "label": "Smooth Driver",
                "style": "bg-sky-900/70 text-sky-300 border-sky-600",
                "score": score,
                "expected": expected_kmpl,
            }
        elif ratio >= 0.85:
            return {
                "label": "Average Pace",
                "style": "bg-amber-900/70 text-amber-300 border-amber-600",
                "score": score,
                "expected": expected_kmpl,
            }
        else:
            return {
                "label": "Heavy Foot",
                "style": "bg-rose-900/70 text-rose-300 border-rose-600",
                "score": score,
                "expected": expected_kmpl,
            }

    def process(self):
        enriched_logs = []
        total_spent = 0.0
        total_fuel = 0.0

        last_full_odometer = None
        buffered_fuel = 0.0
        buffered_cost = 0.0

        valid_mileages = []
        valid_costs_per_km = []
        driver_scores = []

        for log in self.logs:
            entry = dict(log)
            total_spent += entry["total_cost"]
            total_fuel += entry["fuel_liters"]

            entry["route_type"] = entry.get("route_type") or "Balanced Mix"
            entry["traffic_level"] = entry.get("traffic_level") or "Moderate"

            if not entry.get("price_per_liter") or entry["price_per_liter"] <= 0:
                entry["price_per_liter"] = round(
                    entry["total_cost"] / entry["fuel_liters"], 2
                )
            else:
                entry["price_per_liter"] = round(entry["price_per_liter"], 2)

            entry["driving_eval"] = None

            if last_full_odometer is None:
                entry["distance_traveled"] = None
                entry["mileage_kmpl"] = None
                entry["cost_per_km"] = None
                if entry["is_full_tank"]:
                    last_full_odometer = entry["odometer"]
            else:
                buffered_fuel += entry["fuel_liters"]
                buffered_cost += entry["total_cost"]

                if entry["is_full_tank"]:
                    distance = entry["odometer"] - last_full_odometer
                    if distance > 0 and buffered_fuel > 0:
                        kmpl = round(distance / buffered_fuel, 2)
                        cpk = round(buffered_cost / distance, 2)
                        entry["distance_traveled"] = round(distance, 1)
                        entry["mileage_kmpl"] = kmpl
                        entry["cost_per_km"] = cpk

                        eval_data = self.evaluate_driving(
                            kmpl,
                            entry.get("fuel_type", "Diesel"),
                            entry["route_type"],
                            entry["traffic_level"],
                        )
                        entry["driving_eval"] = eval_data
                        driver_scores.append(eval_data["score"])

                        valid_mileages.append(kmpl)
                        valid_costs_per_km.append(cpk)
                    else:
                        entry["distance_traveled"] = 0
                        entry["mileage_kmpl"] = None
                        entry["cost_per_km"] = None

                    last_full_odometer = entry["odometer"]
                    buffered_fuel = 0.0
                    buffered_cost = 0.0
                else:
                    entry["distance_traveled"] = None
                    entry["mileage_kmpl"] = "Buffered (Partial)"
                    entry["cost_per_km"] = None

            enriched_logs.append(entry)

        total_distance = (
            (self.logs[-1]["odometer"] - self.logs[0]["odometer"])
            if len(self.logs) > 1
            else 0.0
        )
        avg_mileage = (
            round(sum(valid_mileages) / len(valid_mileages), 2)
            if valid_mileages
            else 0.0
        )
        avg_cpk = (
            round(sum(valid_costs_per_km) / len(valid_costs_per_km), 2)
            if valid_costs_per_km
            else 0.0
        )
        avg_score = (
            int(round(sum(driver_scores) / len(driver_scores)))
            if driver_scores
            else 0
        )

        if avg_score >= 86:
            overall_rating = "Hypermiler Pro"
        elif avg_score >= 75:
            overall_rating = "Smooth & Efficient"
        elif avg_score >= 65:
            overall_rating = "Balanced Driver"
        elif avg_score > 0:
            overall_rating = "Aggressive / Heavy Foot"
        else:
            overall_rating = "Need 2+ Full Logs"

        summary = {
            "total_spent": round(total_spent, 2),
            "total_fuel": round(total_fuel, 2),
            "total_distance": round(total_distance, 1),
            "avg_mileage": avg_mileage,
            "avg_cost_per_km": avg_cpk,
            "avg_score": avg_score,
            "overall_rating": overall_rating,
        }
        return list(reversed(enriched_logs)), summary


# ==========================================
# 3. HTML TEMPLATE (ANIMATIONS + DASHBOARD)
# ==========================================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>DriveMetrics | Telemetry & Driver Efficiency</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        @keyframes fadeUp {
            from { opacity: 0; transform: translateY(18px); }
            to { opacity: 1; transform: translateY(0); }
        }
        @keyframes slideInRight {
            from { opacity: 0; transform: translateX(-12px); }
            to { opacity: 1; transform: translateX(0); }
        }
        @keyframes pulseHighlight {
            0% { box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); border-color: #10b981; }
            70% { box-shadow: 0 0 0 8px rgba(16, 185, 129, 0); border-color: #10b981; }
            100% { box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }
        }
        .animate-fade-up {
            animation: fadeUp 0.55s cubic-bezier(0.16, 1, 0.3, 1) forwards;
            opacity: 0;
        }
        .animate-row {
            animation: slideInRight 0.4s cubic-bezier(0.16, 1, 0.3, 1) forwards;
            opacity: 0;
        }
        .delay-1 { animation-delay: 0.06s; }
        .delay-2 { animation-delay: 0.12s; }
        .delay-3 { animation-delay: 0.18s; }
        .delay-4 { animation-delay: 0.24s; }
        .delay-5 { animation-delay: 0.30s; }
        .delay-6 { animation-delay: 0.36s; }

        .card-interactive {
            transition: transform 0.28s cubic-bezier(0.16, 1, 0.3, 1),
                        box-shadow 0.28s cubic-bezier(0.16, 1, 0.3, 1),
                        border-color 0.28s ease;
        }
        .card-interactive:hover {
            transform: translateY(-4px);
            box-shadow: 0 14px 28px -10px rgba(2, 6, 23, 0.85),
                        0 0 15px -3px rgba(16, 185, 129, 0.12);
            border-color: rgba(16, 185, 129, 0.45);
        }
        .input-smooth {
            transition: all 0.22s ease;
        }
        .input-smooth:focus {
            transform: translateY(-1px);
            box-shadow: 0 0 0 3px rgba(16, 185, 129, 0.2);
        }
        .auto-filled-flash {
            animation: pulseHighlight 0.75s ease-out;
        }
    </style>
</head>
<body class="bg-slate-900 text-slate-100 min-h-screen font-sans flex flex-col selection:bg-emerald-500 selection:text-white">
    <!-- Top Navbar -->
    <nav class="bg-slate-800/90 backdrop-blur-md sticky top-0 z-20 border-b border-slate-700/80 px-6 py-4 flex justify-between items-center transition-all">
        <a href="{{ url_for('index') }}" class="flex items-center space-x-2.5 group">
            <span class="w-2.5 h-2.5 rounded-full bg-emerald-400 group-hover:scale-125 transition-transform"></span>
            <span class="text-xl font-bold tracking-wider text-emerald-400 group-hover:text-emerald-300 transition-colors">DRIVEMETRICS</span>
            <span class="text-xs bg-slate-700/80 px-2.5 py-1 rounded-full text-slate-300 border border-slate-600">Telemetry & Driver AI</span>
        </a>
        {% if session.get('user_id') %}
        <div class="flex items-center space-x-4">
            <span class="text-sm text-slate-400">Driver: <strong class="text-white">{{ session.get('username') }}</strong></span>
            <a href="{{ url_for('logout') }}" class="text-sm bg-rose-600 hover:bg-rose-500 active:scale-95 px-3.5 py-1.5 rounded-lg transition-all font-medium shadow">Logout</a>
        </div>
        {% endif %}
    </nav>

    <div class="max-w-7xl w-full mx-auto p-6 flex-grow flex flex-col">
        <!-- Flash Notifications -->
        {% with messages = get_flashed_messages(with_categories=true) %}
          {% if messages %}
            {% for category, message in messages %}
              <div class="animate-fade-up mb-4 p-4 rounded-xl text-sm font-medium shadow-lg {% if category == 'error' %}bg-rose-900/60 text-rose-200 border border-rose-700{% else %}bg-emerald-900/60 text-emerald-200 border border-emerald-700{% endif %}">
                {{ message }}
              </div>
            {% endfor %}
          {% endif %}
        {% endwith %}

        {% if auth_page == 'login' %}
        <!-- SEPARATE LOGIN PAGE -->
        <div class="animate-fade-up my-auto mx-auto w-full max-w-md bg-slate-800/90 p-8 rounded-2xl border border-slate-700 shadow-2xl card-interactive">
            <div class="text-center mb-6">
                <h1 class="text-2xl font-bold text-white">Welcome Back</h1>
                <p class="text-sm text-slate-400 mt-1">Sign in to access your vehicle telemetry and driver score</p>
            </div>
            <form method="POST" action="{{ url_for('login') }}" class="space-y-4">
                <div>
                    <label class="block text-xs font-semibold uppercase text-slate-400 mb-1">Username</label>
                    <input type="text" name="username" required placeholder="Enter your username" class="input-smooth w-full bg-slate-900 border border-slate-700 rounded-lg p-3 text-white focus:border-emerald-400 focus:outline-none">
                </div>
                <div>
                    <label class="block text-xs font-semibold uppercase text-slate-400 mb-1">Password</label>
                    <input type="password" name="password" required placeholder="••••••••" class="input-smooth w-full bg-slate-900 border border-slate-700 rounded-lg p-3 text-white focus:border-emerald-400 focus:outline-none">
                </div>
                <button type="submit" class="w-full bg-emerald-600 hover:bg-emerald-500 active:scale-[0.98] text-white font-semibold py-3 rounded-lg transition-all shadow-lg">Sign In</button>
            </form>
            <div class="mt-6 pt-5 border-t border-slate-700 text-center text-sm text-slate-400">
                Don't have an account yet? 
                <a href="{{ url_for('signup') }}" class="text-emerald-400 hover:text-emerald-300 font-semibold underline ml-1 transition-colors">Create an account</a>
            </div>
        </div>

        {% elif auth_page == 'signup' %}
        <!-- SEPARATE SIGNUP PAGE -->
        <div class="animate-fade-up my-auto mx-auto w-full max-w-md bg-slate-800/90 p-8 rounded-2xl border border-slate-700 shadow-2xl card-interactive">
            <div class="text-center mb-6">
                <h1 class="text-2xl font-bold text-white">Create Garage Account</h1>
                <p class="text-sm text-slate-400 mt-1">Track real-world fuel economy, traffic impact & driving skill</p>
            </div>
            <form method="POST" action="{{ url_for('signup') }}" class="space-y-4">
                <div>
                    <label class="block text-xs font-semibold uppercase text-slate-400 mb-1">Choose Username</label>
                    <input type="text" name="username" required placeholder="e.g. faraz_driver" class="input-smooth w-full bg-slate-900 border border-slate-700 rounded-lg p-3 text-white focus:border-sky-400 focus:outline-none">
                </div>
                <div>
                    <label class="block text-xs font-semibold uppercase text-slate-400 mb-1">Password</label>
                    <input type="password" name="password" required placeholder="Create a strong password" class="input-smooth w-full bg-slate-900 border border-slate-700 rounded-lg p-3 text-white focus:border-sky-400 focus:outline-none">
                </div>
                <div>
                    <label class="block text-xs font-semibold uppercase text-slate-400 mb-1">Confirm Password</label>
                    <input type="password" name="confirm_password" required placeholder="Re-enter your password" class="input-smooth w-full bg-slate-900 border border-slate-700 rounded-lg p-3 text-white focus:border-sky-400 focus:outline-none">
                </div>
                <button type="submit" class="w-full bg-sky-600 hover:bg-sky-500 active:scale-[0.98] text-white font-semibold py-3 rounded-lg transition-all shadow-lg">Sign Up</button>
            </form>
            <div class="mt-6 pt-5 border-t border-slate-700 text-center text-sm text-slate-400">
                Already have an account? 
                <a href="{{ url_for('login') }}" class="text-sky-400 hover:text-sky-300 font-semibold underline ml-1 transition-colors">Sign in here</a>
            </div>
        </div>

        {% else %}
        <!-- MAIN DASHBOARD VIEW -->
        <!-- 6 Animated KPI Cards -->
        <div class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4 mb-8">
            <div class="animate-fade-up delay-1 card-interactive bg-slate-800 p-4 rounded-xl border border-slate-700">
                <div class="text-xs text-slate-400 uppercase">Avg Economy</div>
                <div class="text-2xl font-bold text-emerald-400 mt-1">{{ summary.avg_mileage }} <span class="text-xs font-normal text-slate-400">km/L</span></div>
            </div>
            <div class="animate-fade-up delay-2 card-interactive bg-slate-800 p-4 rounded-xl border border-slate-700">
                <div class="text-xs text-slate-400 uppercase">Running Cost</div>
                <div class="text-2xl font-bold text-sky-400 mt-1">₹{{ summary.avg_cost_per_km }} <span class="text-xs font-normal text-slate-400">/km</span></div>
            </div>
            <div class="animate-fade-up delay-3 card-interactive bg-slate-800 p-4 rounded-xl border border-slate-700">
                <div class="text-xs text-slate-400 uppercase">Driver Efficiency</div>
                {% if summary.avg_score > 0 %}
                <div class="text-2xl font-bold text-amber-400 mt-1">{{ summary.avg_score }}<span class="text-xs font-normal text-slate-400">/100</span></div>
                <div class="text-[11px] text-slate-300 truncate">{{ summary.overall_rating }}</div>
                {% else %}
                <div class="text-sm font-semibold text-slate-400 mt-2">{{ summary.overall_rating }}</div>
                {% endif %}
            </div>
            <div class="animate-fade-up delay-4 card-interactive bg-slate-800 p-4 rounded-xl border border-slate-700">
                <div class="text-xs text-slate-400 uppercase">Total Spent</div>
                <div class="text-2xl font-bold text-white mt-1">₹{{ summary.total_spent }}</div>
            </div>
            <div class="animate-fade-up delay-5 card-interactive bg-slate-800 p-4 rounded-xl border border-slate-700">
                <div class="text-xs text-slate-400 uppercase">Tracked Dist.</div>
                <div class="text-2xl font-bold text-white mt-1">{{ summary.total_distance }} <span class="text-xs font-normal text-slate-400">km</span></div>
            </div>
            <div class="animate-fade-up delay-6 card-interactive bg-slate-800 p-4 rounded-xl border border-slate-700">
                <div class="text-xs text-slate-400 uppercase">Total Fuel</div>
                <div class="text-2xl font-bold text-white mt-1">{{ summary.total_fuel }} <span class="text-xs font-normal text-slate-400">L</span></div>
            </div>
        </div>

        <div class="grid lg:grid-cols-12 gap-8">
            <!-- Add OR Edit Fuel Entry Form (4 Cols) -->
            <div class="lg:col-span-4 animate-fade-up delay-2 card-interactive bg-slate-800 p-6 rounded-2xl border {% if edit_entry %}border-amber-500/80{% else %}border-slate-700{% endif %} h-fit">
                <div class="flex justify-between items-center mb-4">
                    <h3 class="text-lg font-semibold {% if edit_entry %}text-amber-400{% else %}text-emerald-400{% endif %}">
                        {{ 'Edit Refuel & Conditions' if edit_entry else 'Log Refuel & Conditions' }}
                    </h3>
                    {% if edit_entry %}
                    <a href="{{ url_for('index') }}" class="text-xs text-slate-400 hover:text-white underline transition-colors">Cancel Edit</a>
                    {% endif %}
                </div>

                <form method="POST" action="{{ url_for('edit_log', log_id=edit_entry.id) if edit_entry else url_for('add_log') }}" class="space-y-4">
                    <div class="grid grid-cols-2 gap-3">
                        <div>
                            <label class="block text-xs text-slate-400 uppercase mb-1">Date</label>
                            <input type="date" name="entry_date" value="{{ edit_entry.entry_date if edit_entry else today }}" required class="input-smooth w-full bg-slate-900 border border-slate-700 rounded-lg p-2 text-sm text-white focus:border-emerald-400 focus:outline-none">
                        </div>
                        <div>
                            <label class="block text-xs text-slate-400 uppercase mb-1">Fuel Grade</label>
                            <select name="fuel_type" class="input-smooth w-full bg-slate-900 border border-slate-700 rounded-lg p-2 text-sm text-white focus:border-emerald-400 focus:outline-none">
                                <option value="Diesel" {% if edit_entry and edit_entry.fuel_type == 'Diesel' %}selected{% endif %}>Diesel</option>
                                <option value="Petrol" {% if edit_entry and edit_entry.fuel_type == 'Petrol' %}selected{% endif %}>Petrol</option>
                            </select>
                        </div>
                    </div>

                    <div>
                        <label class="block text-xs text-slate-400 uppercase mb-1">Current Odometer Reading (km)</label>
                        <input type="number" step="0.1" name="odometer" value="{{ edit_entry.odometer if edit_entry else '' }}" placeholder="e.g. 14250.5" required class="input-smooth w-full bg-slate-900 border border-slate-700 rounded-lg p-2.5 text-sm text-white focus:border-emerald-400 focus:outline-none">
                    </div>

                    <!-- Route Mix & Traffic Conditions -->
                    <div class="p-3.5 bg-slate-900/75 rounded-xl border border-slate-700/90 space-y-3">
                        <div class="text-[11px] text-sky-400 font-semibold uppercase tracking-wider">
                            Driving Conditions (Evaluates Driver Skill)
                        </div>
                        <div>
                            <label class="block text-xs text-slate-400 mb-1">Primary Route Split</label>
                            {% set cur_route = edit_entry.route_type if edit_entry else 'Balanced Mix' %}
                            <select name="route_type" class="input-smooth w-full bg-slate-800 border border-slate-600 rounded-lg p-2 text-sm text-white focus:border-sky-400 focus:outline-none">
                                <option value="100% Highway" {% if cur_route == '100% Highway' %}selected{% endif %}>100% Highway (Open Cruising)</option>
                                <option value="Mostly Highway" {% if cur_route == 'Mostly Highway' %}selected{% endif %}>Mix: Mostly Highway (70% Hwy / 30% City)</option>
                                <option value="Balanced Mix" {% if cur_route == 'Balanced Mix' %}selected{% endif %}>Mix: Balanced (50% Hwy / 50% City)</option>
                                <option value="Mostly City" {% if cur_route == 'Mostly City' %}selected{% endif %}>Mix: Mostly City (70% City / 30% Hwy)</option>
                                <option value="100% City" {% if cur_route == '100% City' %}selected{% endif %}>100% City (Urban Short Trips)</option>
                            </select>
                        </div>
                        <div>
                            <label class="block text-xs text-slate-400 mb-1">Traffic Density Encountered</label>
                            {% set cur_traffic = edit_entry.traffic_level if edit_entry else 'Moderate' %}
                            <select name="traffic_level" class="input-smooth w-full bg-slate-800 border border-slate-600 rounded-lg p-2 text-sm text-white focus:border-sky-400 focus:outline-none">
                                <option value="Free Flow" {% if cur_traffic == 'Free Flow' %}selected{% endif %}>Free Flow (Zero Delays)</option>
                                <option value="Light" {% if cur_traffic == 'Light' %}selected{% endif %}>Light Traffic (Smooth Flow)</option>
                                <option value="Moderate" {% if cur_traffic == 'Moderate' %}selected{% endif %}>Moderate Traffic (Normal Signals)</option>
                                <option value="Heavy" {% if cur_traffic == 'Heavy' %}selected{% endif %}>Heavy Traffic (Frequent Slowdowns)</option>
                                <option value="Gridlock" {% if cur_traffic == 'Gridlock' %}selected{% endif %}>Gridlock (Bumper-to-Bumper / 1st-2nd Gear)</option>
                            </select>
                        </div>
                    </div>

                    <!-- 3-Way Auto-Calculation Fields -->
                    <div class="p-3.5 bg-slate-900/75 rounded-xl border border-slate-700/90 space-y-3">
                        <div class="text-[11px] text-emerald-400 font-medium">
                            Smart Fill: Enter any 2 values — the 3rd calculates automatically!
                        </div>
                        <div class="grid grid-cols-3 gap-2">
                            <div>
                                <label class="block text-[11px] text-slate-400 uppercase mb-1">Fuel (L)</label>
                                <input type="number" step="0.01" id="fuel_liters" name="fuel_liters" value="{{ edit_entry.fuel_liters if edit_entry else '' }}" placeholder="40.0" required class="input-smooth w-full bg-slate-800 border border-slate-600 rounded-lg p-2 text-sm text-white focus:border-emerald-400 focus:outline-none">
                            </div>
                            <div>
                                <label class="block text-[11px] text-slate-400 uppercase mb-1">Rate (₹/L)</label>
                                <input type="number" step="0.01" id="price_per_liter" name="price_per_liter" value="{{ edit_entry.price_per_liter if edit_entry else '' }}" placeholder="92.5" required class="input-smooth w-full bg-slate-800 border border-slate-600 rounded-lg p-2 text-sm text-white focus:border-emerald-400 focus:outline-none">
                            </div>
                            <div>
                                <label class="block text-[11px] text-slate-400 uppercase mb-1">Total (₹)</label>
                                <input type="number" step="0.01" id="total_cost" name="total_cost" value="{{ edit_entry.total_cost if edit_entry else '' }}" placeholder="3700" required class="input-smooth w-full bg-slate-800 border border-slate-600 rounded-lg p-2 text-sm text-white focus:border-emerald-400 focus:outline-none">
                            </div>
                        </div>
                    </div>

                    <div class="flex items-center space-x-2 pt-1">
                        <input type="checkbox" name="is_full_tank" id="full_tank" {% if not edit_entry or edit_entry.is_full_tank %}checked{% endif %} class="w-4 h-4 accent-emerald-500 rounded cursor-pointer">
                        <label for="full_tank" class="text-sm text-slate-300 cursor-pointer">Full Tank Fill-up (Tank-to-Tank reset)</label>
                    </div>
                    <div>
                        <label class="block text-xs text-slate-400 uppercase mb-1">Notes (Optional)</label>
                        <input type="text" name="notes" value="{{ edit_entry.notes if edit_entry else '' }}" placeholder="e.g. AC on, mountain climb, cruise control" class="input-smooth w-full bg-slate-900 border border-slate-700 rounded-lg p-2 text-sm text-white focus:border-emerald-400 focus:outline-none">
                    </div>
                    <button type="submit" class="w-full {% if edit_entry %}bg-amber-600 hover:bg-amber-500{% else %}bg-emerald-600 hover:bg-emerald-500{% endif %} active:scale-[0.98] text-white font-semibold py-2.5 rounded-lg transition-all shadow-lg">
                        {{ 'Update Entry & Recalculate' if edit_entry else 'Save Telemetry Entry' }}
                    </button>
                </form>
            </div>

            <!-- Log History Table (8 Cols) -->
            <div class="lg:col-span-8 animate-fade-up delay-3 bg-slate-800 p-6 rounded-2xl border border-slate-700 overflow-x-auto shadow-xl">
                <div class="flex justify-between items-center mb-4">
                    <h3 class="text-lg font-semibold text-white">Refueling, Route & Driver Assessment History</h3>
                    <span class="text-xs text-slate-400">Mileage is judged against route & traffic difficulty</span>
                </div>
                {% if logs %}
                <table class="w-full text-left border-collapse text-sm">
                    <thead>
                        <tr class="border-b border-slate-700 text-slate-400 text-xs uppercase">
                            <th class="py-3 px-2">Date / Grade</th>
                            <th class="py-3 px-2">Odometer</th>
                            <th class="py-3 px-2">Conditions</th>
                            <th class="py-3 px-2">Fuel & Cost</th>
                            <th class="py-3 px-2">Mileage</th>
                            <th class="py-3 px-2">Driver Assessment</th>
                            <th class="py-3 px-2">Notes</th>
                            <th class="py-3 px-2 text-right">Actions</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-slate-700/60">
                        {% for log in logs %}
                        <tr class="animate-row hover:bg-slate-700/40 transition-colors {% if edit_entry and edit_entry.id == log.id %}bg-amber-900/20{% endif %}" style="animation-delay: {{ loop.index0 * 0.05 }}s">
                            <td class="py-3.5 px-2 whitespace-nowrap">
                                <div class="font-medium text-white">{{ log.entry_date }}</div>
                                <span class="inline-block mt-1 text-[11px] px-2 py-0.5 rounded font-medium {% if log.fuel_type == 'Diesel' %}bg-sky-900/70 text-sky-300 border border-sky-700{% else %}bg-amber-900/70 text-amber-300 border border-amber-700{% endif %}">
                                    {{ log.fuel_type }}
                                </span>
                            </td>
                            <td class="py-3.5 px-2 font-mono whitespace-nowrap">
                                <div>{{ log.odometer }} km</div>
                                <div class="text-xs text-slate-400">{{ ('+' ~ log.distance_traveled ~ ' km') if log.distance_traveled else 'Baseline' }}</div>
                            </td>
                            <td class="py-3.5 px-2 whitespace-nowrap">
                                <div class="text-xs font-medium text-slate-200">{{ log.route_type }}</div>
                                <div class="text-[11px] mt-0.5 {% if log.traffic_level in ['Heavy', 'Gridlock'] %}text-rose-400{% elif log.traffic_level in ['Free Flow', 'Light'] %}text-emerald-400{% else %}text-amber-300{% endif %}">
                                    Traffic: {{ log.traffic_level }}
                                </div>
                            </td>
                            <td class="py-3.5 px-2 whitespace-nowrap">
                                <div class="font-medium text-white">{{ log.fuel_liters }} L <span class="text-slate-400 font-normal">• ₹{{ log.total_cost }}</span></div>
                                <div class="text-xs text-slate-400">@ ₹{{ log.price_per_liter }}/L {% if log.cost_per_km %}• ₹{{ log.cost_per_km }}/km{% endif %}</div>
                            </td>
                            <td class="py-3.5 px-2 font-semibold text-emerald-400 whitespace-nowrap">
                                {% if log.mileage_kmpl is number %}
                                    <div class="text-base">{{ log.mileage_kmpl }} km/L</div>
                                    {% if log.driving_eval %}
                                    <div class="text-[11px] font-normal text-slate-400">Target: {{ log.driving_eval.expected }} km/L</div>
                                    {% endif %}
                                {% elif log.mileage_kmpl %}
                                    <span class="text-xs text-amber-400">{{ log.mileage_kmpl }}</span>
                                {% else %}
                                    <span class="text-xs text-slate-500">Baseline Anchor</span>
                                {% endif %}
                            </td>
                            <td class="py-3.5 px-2 whitespace-nowrap">
                                {% if log.driving_eval %}
                                    <span class="inline-flex items-center px-2.5 py-1 rounded-full text-xs font-semibold border {{ log.driving_eval.style }}">
                                        {{ log.driving_eval.label }} ({{ log.driving_eval.score }}/100)
                                    </span>
                                {% else %}
                                    <span class="text-xs text-slate-500">—</span>
                                {% endif %}
                            </td>
                            <td class="py-3.5 px-2 text-slate-400 text-xs max-w-[140px] truncate" title="{{ log.notes or '' }}">{{ log.notes or '' }}</td>
                            <td class="py-3.5 px-2 text-right whitespace-nowrap space-x-2">
                                <a href="{{ url_for('index', edit_id=log.id) }}" class="text-amber-400 hover:text-amber-300 text-xs font-medium transition-colors">Edit</a>
                                <form method="POST" action="{{ url_for('delete_log', log_id=log.id) }}" class="inline" onsubmit="return confirm('Delete this fuel log?');">
                                    <button type="submit" class="text-rose-400 hover:text-rose-300 text-xs font-medium transition-colors">Delete</button>
                                </form>
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
                {% else %}
                <p class="text-slate-400 text-sm py-6 text-center">No fuel entries logged yet. Add your first full-tank baseline entry on the left to start tracking.</p>
                {% endif %}
            </div>
        </div>

        <!-- Real-Time 3-Way Auto-Calculation Script with Smooth Visual Pulse -->
        <script>
            const litersInput = document.getElementById('fuel_liters');
            const rateInput = document.getElementById('price_per_liter');
            const totalInput = document.getElementById('total_cost');

            let editHistory = [];

            function recordEdit(fieldName) {
                editHistory = editHistory.filter(f => f !== fieldName);
                editHistory.push(fieldName);
                if (editHistory.length > 2) {
                    editHistory.shift();
                }
            }

            function flashField(element) {
                element.classList.remove('auto-filled-flash');
                void element.offsetWidth; // trigger reflow for smooth re-animation
                element.classList.add('auto-filled-flash');
            }

            function autoCalculate(changedField) {
                recordEdit(changedField);

                const L = parseFloat(litersInput.value);
                const R = parseFloat(rateInput.value);
                const T = parseFloat(totalInput.value);

                if (editHistory.includes('liters') && editHistory.includes('rate')) {
                    if (L > 0 && R > 0) {
                        totalInput.value = (L * R).toFixed(2);
                        flashField(totalInput);
                    }
                } else if (editHistory.includes('liters') && editHistory.includes('total')) {
                    if (L > 0 && T > 0) {
                        rateInput.value = (T / L).toFixed(2);
                        flashField(rateInput);
                    }
                } else if (editHistory.includes('rate') && editHistory.includes('total')) {
                    if (R > 0 && T > 0) {
                        litersInput.value = (T / R).toFixed(2);
                        flashField(litersInput);
                    }
                } else {
                    if ((changedField === 'liters' || changedField === 'rate') && L > 0 && R > 0) {
                        totalInput.value = (L * R).toFixed(2);
                        flashField(totalInput);
                    } else if (changedField === 'total' && T > 0 && R > 0) {
                        litersInput.value = (T / R).toFixed(2);
                        flashField(litersInput);
                    } else if (changedField === 'total' && T > 0 && L > 0) {
                        rateInput.value = (T / L).toFixed(2);
                        flashField(rateInput);
                    }
                }
            }

            if (litersInput && rateInput && totalInput) {
                litersInput.addEventListener('input', () => autoCalculate('liters'));
                rateInput.addEventListener('input', () => autoCalculate('rate'));
                totalInput.addEventListener('input', () => autoCalculate('total'));
            }
        </script>
        {% endif %}
    </div>
</body>
</html>
"""


# ==========================================
# 4. HELPER FOR SERVER-SIDE 3-WAY FALLBACK
# ==========================================
def resolve_fuel_math(liters_raw, rate_raw, total_raw):
    liters = float(liters_raw) if liters_raw else 0.0
    rate = float(rate_raw) if rate_raw else 0.0
    total = float(total_raw) if total_raw else 0.0

    if liters > 0 and rate > 0 and total <= 0:
        total = round(liters * rate, 2)
    elif liters > 0 and total > 0 and rate <= 0:
        rate = round(total / liters, 2)
    elif rate > 0 and total > 0 and liters <= 0:
        liters = round(total / rate, 2)

    return round(liters, 2), round(rate, 2), round(total, 2)


# ==========================================
# 5. APPLICATION ROUTES & CONTROLLERS
# ==========================================
@app.route("/")
def index():
    if "user_id" not in session:
        return redirect(url_for("login"))

    db = get_db()
    raw_logs = db.execute(
        "SELECT * FROM fuel_logs WHERE user_id = ? ORDER BY odometer ASC",
        (session["user_id"],),
    ).fetchall()

    edit_id = request.args.get("edit_id", type=int)
    edit_entry = None
    if edit_id:
        edit_entry = db.execute(
            "SELECT * FROM fuel_logs WHERE id = ? AND user_id = ?",
            (edit_id, session["user_id"]),
        ).fetchone()

    analyzer = MileageAnalyzer(raw_logs)
    logs, summary = analyzer.process()
    today = datetime.today().strftime("%Y-%m-%d")

    return render_template_string(
        HTML_TEMPLATE,
        auth_page=None,
        logs=logs,
        summary=summary,
        today=today,
        edit_entry=edit_entry,
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        db = get_db()
        user = db.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()

        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            flash(f"Welcome back, {user['username']}!", "success")
            return redirect(url_for("index"))
        else:
            flash("Invalid username or password.", "error")

    return render_template_string(HTML_TEMPLATE, auth_page="login")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if "user_id" in session:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not username or not password:
            flash("Username and password are required.", "error")
            return redirect(url_for("signup"))

        if password != confirm_password:
            flash("Passwords do not match.", "error")
            return redirect(url_for("signup"))

        db = get_db()
        try:
            db.execute(
                "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                (username, generate_password_hash(password)),
            )
            db.commit()
            flash("Account created! Please sign in with your credentials.", "success")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError:
            flash("Username already taken. Please choose another.", "error")

    return render_template_string(HTML_TEMPLATE, auth_page="signup")


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


@app.route("/add", methods=["POST"])
def add_log():
    if "user_id" not in session:
        return redirect(url_for("login"))

    try:
        entry_date = request.form["entry_date"]
        fuel_type = request.form.get("fuel_type", "Diesel")
        if fuel_type not in ("Diesel", "Petrol"):
            fuel_type = "Diesel"

        route_type = request.form.get("route_type", "Balanced Mix")
        traffic_level = request.form.get("traffic_level", "Moderate")

        odometer = float(request.form["odometer"])
        fuel_liters, price_per_liter, total_cost = resolve_fuel_math(
            request.form.get("fuel_liters"),
            request.form.get("price_per_liter"),
            request.form.get("total_cost"),
        )
        is_full_tank = 1 if request.form.get("is_full_tank") else 0
        notes = request.form.get("notes", "").strip()

        if odometer <= 0 or fuel_liters <= 0 or total_cost <= 0:
            flash(
                "Odometer and at least two fuel cost/volume values must be positive.",
                "error",
            )
            return redirect(url_for("index"))

        db = get_db()
        db.execute(
            """
            INSERT INTO fuel_logs (
                user_id, entry_date, fuel_type, route_type, traffic_level,
                odometer, fuel_liters, price_per_liter, total_cost, is_full_tank, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session["user_id"],
                entry_date,
                fuel_type,
                route_type,
                traffic_level,
                odometer,
                fuel_liters,
                price_per_liter,
                total_cost,
                is_full_tank,
                notes,
            ),
        )
        db.commit()
        flash("Refueling entry and driving conditions recorded.", "success")
    except ValueError:
        flash("Invalid numerical values entered.", "error")

    return redirect(url_for("index"))


@app.route("/edit/<int:log_id>", methods=["POST"])
def edit_log(log_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    try:
        entry_date = request.form["entry_date"]
        fuel_type = request.form.get("fuel_type", "Diesel")
        if fuel_type not in ("Diesel", "Petrol"):
            fuel_type = "Diesel"

        route_type = request.form.get("route_type", "Balanced Mix")
        traffic_level = request.form.get("traffic_level", "Moderate")

        odometer = float(request.form["odometer"])
        fuel_liters, price_per_liter, total_cost = resolve_fuel_math(
            request.form.get("fuel_liters"),
            request.form.get("price_per_liter"),
            request.form.get("total_cost"),
        )
        is_full_tank = 1 if request.form.get("is_full_tank") else 0
        notes = request.form.get("notes", "").strip()

        if odometer <= 0 or fuel_liters <= 0 or total_cost <= 0:
            flash("Odometer, fuel volume, and cost must be positive values.", "error")
            return redirect(url_for("index", edit_id=log_id))

        db = get_db()
        db.execute(
            """
            UPDATE fuel_logs
            SET entry_date = ?, fuel_type = ?, route_type = ?, traffic_level = ?,
                odometer = ?, fuel_liters = ?, price_per_liter = ?, total_cost = ?,
                is_full_tank = ?, notes = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                entry_date,
                fuel_type,
                route_type,
                traffic_level,
                odometer,
                fuel_liters,
                price_per_liter,
                total_cost,
                is_full_tank,
                notes,
                log_id,
                session["user_id"],
            ),
        )
        db.commit()
        flash("Entry updated and driver assessment recalculated.", "success")
    except ValueError:
        flash("Invalid numerical values entered.", "error")

    return redirect(url_for("index"))


@app.route("/delete/<int:log_id>", methods=["POST"])
def delete_log(log_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    db = get_db()
    db.execute(
        "DELETE FROM fuel_logs WHERE id = ? AND user_id = ?",
        (log_id, session["user_id"]),
    )
    db.commit()
    flash("Entry deleted and telemetry recalculated.", "success")
    return redirect(url_for("index"))


if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5000)

    # note: in production, set debug=False and use a proper WSGI server like Gunicorn or uWSGI