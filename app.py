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
                odometer REAL NOT NULL,
                fuel_liters REAL NOT NULL,
                price_per_liter REAL NOT NULL DEFAULT 0.0,
                total_cost REAL NOT NULL,
                is_full_tank INTEGER NOT NULL DEFAULT 1,
                notes TEXT,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            )
        """)
        # Safe migration in case an older database file already exists
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
        db.commit()


# ==========================================
# 2. OOP ANALYTICS & CALCULATION ENGINE
# ==========================================
class MileageAnalyzer:
    """Calculates tank-to-tank mileage and buffers partial fill-ups
    until the next full tank is recorded.
    """

    def __init__(self, logs):
        self.logs = sorted(logs, key=lambda x: x["odometer"])

    def process(self):
        enriched_logs = []
        total_spent = 0.0
        total_fuel = 0.0

        last_full_odometer = None
        buffered_fuel = 0.0
        buffered_cost = 0.0

        valid_mileages = []
        valid_costs_per_km = []

        for log in self.logs:
            entry = dict(log)
            total_spent += entry["total_cost"]
            total_fuel += entry["fuel_liters"]

            # Ensure price_per_liter is always accurate
            if not entry.get("price_per_liter") or entry["price_per_liter"] <= 0:
                entry["price_per_liter"] = round(
                    entry["total_cost"] / entry["fuel_liters"], 2
                )
            else:
                entry["price_per_liter"] = round(entry["price_per_liter"], 2)

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

        summary = {
            "total_spent": round(total_spent, 2),
            "total_fuel": round(total_fuel, 2),
            "total_distance": round(total_distance, 1),
            "avg_mileage": avg_mileage,
            "avg_cost_per_km": avg_cpk,
        }
        return list(reversed(enriched_logs)), summary


# ==========================================
# 3. HTML TEMPLATE (AUTH + DASHBOARD + JS)
# ==========================================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>DriveMetrics | Fuel & Mileage Tracker</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-900 text-slate-100 min-h-screen font-sans flex flex-col">
    <!-- Top Navbar -->
    <nav class="bg-slate-800 border-b border-slate-700 px-6 py-4 flex justify-between items-center">
        <a href="{{ url_for('index') }}" class="flex items-center space-x-2">
            <span class="text-xl font-bold tracking-wider text-emerald-400">DRIVEMETRICS</span>
            <span class="text-xs bg-slate-700 px-2 py-1 rounded text-slate-300">Telemetry & Fuel Log</span>
        </a>
        {% if session.get('user_id') %}
        <div class="flex items-center space-x-4">
            <span class="text-sm text-slate-400">Driver: <strong class="text-white">{{ session.get('username') }}</strong></span>
            <a href="{{ url_for('logout') }}" class="text-sm bg-rose-600 hover:bg-rose-500 px-3 py-1.5 rounded transition font-medium">Logout</a>
        </div>
        {% endif %}
    </nav>

    <div class="max-w-6xl w-full mx-auto p-6 flex-grow flex flex-col">
        <!-- Flash Notifications -->
        {% with messages = get_flashed_messages(with_categories=true) %}
          {% if messages %}
            {% for category, message in messages %}
              <div class="mb-4 p-4 rounded-lg text-sm font-medium {% if category == 'error' %}bg-rose-900/60 text-rose-200 border border-rose-700{% else %}bg-emerald-900/60 text-emerald-200 border border-emerald-700{% endif %}">
                {{ message }}
              </div>
            {% endfor %}
          {% endif %}
        {% endwith %}

        {% if auth_page == 'login' %}
        <!-- SEPARATE LOGIN PAGE -->
        <div class="my-auto mx-auto w-full max-w-md bg-slate-800 p-8 rounded-2xl border border-slate-700 shadow-2xl">
            <div class="text-center mb-6">
                <h1 class="text-2xl font-bold text-white">Welcome Back</h1>
                <p class="text-sm text-slate-400 mt-1">Sign in to access your vehicle telemetry and fuel logs</p>
            </div>
            <form method="POST" action="{{ url_for('login') }}" class="space-y-4">
                <div>
                    <label class="block text-xs font-semibold uppercase text-slate-400 mb-1">Username</label>
                    <input type="text" name="username" required placeholder="Enter your username" class="w-full bg-slate-900 border border-slate-700 rounded-lg p-3 text-white focus:border-emerald-400 focus:outline-none">
                </div>
                <div>
                    <label class="block text-xs font-semibold uppercase text-slate-400 mb-1">Password</label>
                    <input type="password" name="password" required placeholder="••••••••" class="w-full bg-slate-900 border border-slate-700 rounded-lg p-3 text-white focus:border-emerald-400 focus:outline-none">
                </div>
                <button type="submit" class="w-full bg-emerald-600 hover:bg-emerald-500 text-white font-semibold py-3 rounded-lg transition shadow-lg">Sign In</button>
            </form>
            <div class="mt-6 pt-5 border-t border-slate-700 text-center text-sm text-slate-400">
                Don't have an account yet? 
                <a href="{{ url_for('signup') }}" class="text-emerald-400 hover:text-emerald-300 font-semibold underline ml-1">Create an account</a>
            </div>
        </div>

        {% elif auth_page == 'signup' %}
        <!-- SEPARATE SIGNUP PAGE -->
        <div class="my-auto mx-auto w-full max-w-md bg-slate-800 p-8 rounded-2xl border border-slate-700 shadow-2xl">
            <div class="text-center mb-6">
                <h1 class="text-2xl font-bold text-white">Create Garage Account</h1>
                <p class="text-sm text-slate-400 mt-1">Start tracking your vehicle's real-world fuel economy</p>
            </div>
            <form method="POST" action="{{ url_for('signup') }}" class="space-y-4">
                <div>
                    <label class="block text-xs font-semibold uppercase text-slate-400 mb-1">Choose Username</label>
                    <input type="text" name="username" required placeholder="e.g. faraz_driver" class="w-full bg-slate-900 border border-slate-700 rounded-lg p-3 text-white focus:border-sky-400 focus:outline-none">
                </div>
                <div>
                    <label class="block text-xs font-semibold uppercase text-slate-400 mb-1">Password</label>
                    <input type="password" name="password" required placeholder="Create a strong password" class="w-full bg-slate-900 border border-slate-700 rounded-lg p-3 text-white focus:border-sky-400 focus:outline-none">
                </div>
                <div>
                    <label class="block text-xs font-semibold uppercase text-slate-400 mb-1">Confirm Password</label>
                    <input type="password" name="confirm_password" required placeholder="Re-enter your password" class="w-full bg-slate-900 border border-slate-700 rounded-lg p-3 text-white focus:border-sky-400 focus:outline-none">
                </div>
                <button type="submit" class="w-full bg-sky-600 hover:bg-sky-500 text-white font-semibold py-3 rounded-lg transition shadow-lg">Sign Up</button>
            </form>
            <div class="mt-6 pt-5 border-t border-slate-700 text-center text-sm text-slate-400">
                Already have an account? 
                <a href="{{ url_for('login') }}" class="text-sky-400 hover:text-sky-300 font-semibold underline ml-1">Sign in here</a>
            </div>
        </div>

        {% else %}
        <!-- MAIN DASHBOARD VIEW -->
        <div class="grid grid-cols-2 md:grid-cols-5 gap-4 mb-8">
            <div class="bg-slate-800 p-4 rounded-xl border border-slate-700">
                <div class="text-xs text-slate-400 uppercase">Avg Fuel Economy</div>
                <div class="text-2xl font-bold text-emerald-400 mt-1">{{ summary.avg_mileage }} <span class="text-sm font-normal text-slate-400">km/L</span></div>
            </div>
            <div class="bg-slate-800 p-4 rounded-xl border border-slate-700">
                <div class="text-xs text-slate-400 uppercase">Running Cost</div>
                <div class="text-2xl font-bold text-sky-400 mt-1">₹{{ summary.avg_cost_per_km }} <span class="text-sm font-normal text-slate-400">/km</span></div>
            </div>
            <div class="bg-slate-800 p-4 rounded-xl border border-slate-700">
                <div class="text-xs text-slate-400 uppercase">Total Spent</div>
                <div class="text-2xl font-bold text-white mt-1">₹{{ summary.total_spent }}</div>
            </div>
            <div class="bg-slate-800 p-4 rounded-xl border border-slate-700">
                <div class="text-xs text-slate-400 uppercase">Tracked Distance</div>
                <div class="text-2xl font-bold text-white mt-1">{{ summary.total_distance }} <span class="text-sm font-normal text-slate-400">km</span></div>
            </div>
            <div class="bg-slate-800 p-4 rounded-xl border border-slate-700">
                <div class="text-xs text-slate-400 uppercase">Total Fuel Consumed</div>
                <div class="text-2xl font-bold text-white mt-1">{{ summary.total_fuel }} <span class="text-sm font-normal text-slate-400">L</span></div>
            </div>
        </div>

        <div class="grid lg:grid-cols-3 gap-8">
            <!-- Add OR Edit Fuel Entry Form -->
            <div class="bg-slate-800 p-6 rounded-xl border {% if edit_entry %}border-amber-500/80{% else %}border-slate-700{% endif %} h-fit">
                <div class="flex justify-between items-center mb-4">
                    <h3 class="text-lg font-semibold {% if edit_entry %}text-amber-400{% else %}text-emerald-400{% endif %}">
                        {{ 'Edit Refuel Entry' if edit_entry else 'Log Refuel' }}
                    </h3>
                    {% if edit_entry %}
                    <a href="{{ url_for('index') }}" class="text-xs text-slate-400 hover:text-white underline">Cancel Edit</a>
                    {% endif %}
                </div>

                <form method="POST" action="{{ url_for('edit_log', log_id=edit_entry.id) if edit_entry else url_for('add_log') }}" class="space-y-4">
                    <div class="grid grid-cols-2 gap-3">
                        <div>
                            <label class="block text-xs text-slate-400 uppercase mb-1">Date</label>
                            <input type="date" name="entry_date" value="{{ edit_entry.entry_date if edit_entry else today }}" required class="w-full bg-slate-900 border border-slate-700 rounded p-2 text-sm text-white">
                        </div>
                        <div>
                            <label class="block text-xs text-slate-400 uppercase mb-1">Fuel Type</label>
                            <select name="fuel_type" class="w-full bg-slate-900 border border-slate-700 rounded p-2 text-sm text-white focus:border-emerald-400 focus:outline-none">
                                <option value="Diesel" {% if edit_entry and edit_entry.fuel_type == 'Diesel' %}selected{% endif %}>Diesel</option>
                                <option value="Petrol" {% if edit_entry and edit_entry.fuel_type == 'Petrol' %}selected{% endif %}>Petrol</option>
                            </select>
                        </div>
                    </div>

                    <div>
                        <label class="block text-xs text-slate-400 uppercase mb-1">Current Odometer Reading (km)</label>
                        <input type="number" step="0.1" name="odometer" value="{{ edit_entry.odometer if edit_entry else '' }}" placeholder="e.g. 12450.5" required class="w-full bg-slate-900 border border-slate-700 rounded p-2 text-sm text-white">
                    </div>

                    <!-- 3-Way Auto-Calculation Fields -->
                    <div class="p-3 bg-slate-900/70 rounded-lg border border-slate-700/80 space-y-3">
                        <div class="text-[11px] text-emerald-400 font-medium">
                            Smart Fill: Enter any 2 values below — the 3rd calculates automatically!
                        </div>
                        <div class="grid grid-cols-3 gap-2">
                            <div>
                                <label class="block text-[11px] text-slate-400 uppercase mb-1">Fuel (Liters)</label>
                                <input type="number" step="0.01" id="fuel_liters" name="fuel_liters" value="{{ edit_entry.fuel_liters if edit_entry else '' }}" placeholder="40.0" required class="w-full bg-slate-800 border border-slate-600 rounded p-2 text-sm text-white focus:border-emerald-400 focus:outline-none">
                            </div>
                            <div>
                                <label class="block text-[11px] text-slate-400 uppercase mb-1">Rate (₹/L)</label>
                                <input type="number" step="0.01" id="price_per_liter" name="price_per_liter" value="{{ edit_entry.price_per_liter if edit_entry else '' }}" placeholder="92.5" required class="w-full bg-slate-800 border border-slate-600 rounded p-2 text-sm text-white focus:border-emerald-400 focus:outline-none">
                            </div>
                            <div>
                                <label class="block text-[11px] text-slate-400 uppercase mb-1">Total (₹)</label>
                                <input type="number" step="0.01" id="total_cost" name="total_cost" value="{{ edit_entry.total_cost if edit_entry else '' }}" placeholder="3700" required class="w-full bg-slate-800 border border-slate-600 rounded p-2 text-sm text-white focus:border-emerald-400 focus:outline-none">
                            </div>
                        </div>
                    </div>

                    <div class="flex items-center space-x-2 pt-1">
                        <input type="checkbox" name="is_full_tank" id="full_tank" {% if not edit_entry or edit_entry.is_full_tank %}checked{% endif %} class="w-4 h-4 accent-emerald-500">
                        <label for="full_tank" class="text-sm text-slate-300">Full Tank Fill-up (Tank-to-Tank reset)</label>
                    </div>
                    <div>
                        <label class="block text-xs text-slate-400 uppercase mb-1">Route / Notes (Optional)</label>
                        <input type="text" name="notes" value="{{ edit_entry.notes if edit_entry else '' }}" placeholder="e.g. Highway commute / City traffic" class="w-full bg-slate-900 border border-slate-700 rounded p-2 text-sm text-white">
                    </div>
                    <button type="submit" class="w-full {% if edit_entry %}bg-amber-600 hover:bg-amber-500{% else %}bg-emerald-600 hover:bg-emerald-500{% endif %} text-white font-semibold py-2.5 rounded transition">
                        {{ 'Update Entry' if edit_entry else 'Save Entry' }}
                    </button>
                </form>
            </div>

            <!-- Log History Table -->
            <div class="lg:col-span-2 bg-slate-800 p-6 rounded-xl border border-slate-700 overflow-x-auto">
                <h3 class="text-lg font-semibold mb-4 text-white">Refueling & Telemetry History</h3>
                {% if logs %}
                <table class="w-full text-left border-collapse text-sm">
                    <thead>
                        <tr class="border-b border-slate-700 text-slate-400 text-xs uppercase">
                            <th class="py-3 px-2">Date</th>
                            <th class="py-3 px-2">Grade</th>
                            <th class="py-3 px-2">Odometer</th>
                            <th class="py-3 px-2">Dist.</th>
                            <th class="py-3 px-2">Fuel</th>
                            <th class="py-3 px-2">Total Cost</th>
                            <th class="py-3 px-2">Mileage</th>
                            <th class="py-3 px-2">Cost/km</th>
                            <th class="py-3 px-2">Notes</th>
                            <th class="py-3 px-2 text-right">Actions</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-slate-700/60">
                        {% for log in logs %}
                        <tr class="hover:bg-slate-700/30 {% if edit_entry and edit_entry.id == log.id %}bg-amber-900/20{% endif %}">
                            <td class="py-3 px-2 whitespace-nowrap">{{ log.entry_date }}</td>
                            <td class="py-3 px-2">
                                <span class="text-xs px-2 py-0.5 rounded font-medium {% if log.fuel_type == 'Diesel' %}bg-sky-900/70 text-sky-300 border border-sky-700{% else %}bg-amber-900/70 text-amber-300 border border-amber-700{% endif %}">
                                    {{ log.fuel_type }}
                                </span>
                            </td>
                            <td class="py-3 px-2 font-mono whitespace-nowrap">{{ log.odometer }} km</td>
                            <td class="py-3 px-2 whitespace-nowrap">{{ log.distance_traveled ~ ' km' if log.distance_traveled else '—' }}</td>
                            <td class="py-3 px-2 whitespace-nowrap">{{ log.fuel_liters }} L</td>
                            <td class="py-3 px-2 whitespace-nowrap">
                                ₹{{ log.total_cost }}
                                <span class="text-xs text-slate-400 block">@ ₹{{ log.price_per_liter }}/L</span>
                            </td>
                            <td class="py-3 px-2 font-semibold text-emerald-400 whitespace-nowrap">
                                {% if log.mileage_kmpl is number %}
                                    {{ log.mileage_kmpl }} km/L
                                {% elif log.mileage_kmpl %}
                                    <span class="text-xs text-amber-400">{{ log.mileage_kmpl }}</span>
                                {% else %}
                                    <span class="text-xs text-slate-500">Baseline</span>
                                {% endif %}
                            </td>
                            <td class="py-3 px-2 whitespace-nowrap">{{ '₹' ~ log.cost_per_km if log.cost_per_km else '—' }}</td>
                            <td class="py-3 px-2 text-slate-400 text-xs">{{ log.notes or '' }}</td>
                            <td class="py-3 px-2 text-right whitespace-nowrap space-x-2">
                                <a href="{{ url_for('index', edit_id=log.id) }}" class="text-amber-400 hover:text-amber-300 text-xs font-medium">Edit</a>
                                <form method="POST" action="{{ url_for('delete_log', log_id=log.id) }}" class="inline" onsubmit="return confirm('Delete this fuel log?');">
                                    <button type="submit" class="text-rose-400 hover:text-rose-300 text-xs font-medium">Delete</button>
                                </form>
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
                {% else %}
                <p class="text-slate-400 text-sm">No fuel entries logged yet. Add your first full-tank baseline entry on the left to begin tracking.</p>
                {% endif %}
            </div>
        </div>

        <!-- Real-Time 3-Way Auto-Calculation Script -->
        <script>
            const litersInput = document.getElementById('fuel_liters');
            const rateInput = document.getElementById('price_per_liter');
            const totalInput = document.getElementById('total_cost');

            // Tracks the last two fields edited by the user so the 3rd field is derived automatically
            let editHistory = [];

            function recordEdit(fieldName) {
                editHistory = editHistory.filter(f => f !== fieldName);
                editHistory.push(fieldName);
                if (editHistory.length > 2) {
                    editHistory.shift();
                }
            }

            function autoCalculate(changedField) {
                recordEdit(changedField);

                const L = parseFloat(litersInput.value);
                const R = parseFloat(rateInput.value);
                const T = parseFloat(totalInput.value);

                // Determine which target field to compute based on the last two edited fields
                if (editHistory.includes('liters') && editHistory.includes('rate')) {
                    if (L > 0 && R > 0) totalInput.value = (L * R).toFixed(2);
                } else if (editHistory.includes('liters') && editHistory.includes('total')) {
                    if (L > 0 && T > 0) rateInput.value = (T / L).toFixed(2);
                } else if (editHistory.includes('rate') && editHistory.includes('total')) {
                    if (R > 0 && T > 0) litersInput.value = (T / R).toFixed(2);
                } else {
                    // Fallback when editing a form that already has values pre-populated
                    if (changedField === 'liters' && L > 0 && R > 0) {
                        totalInput.value = (L * R).toFixed(2);
                    } else if (changedField === 'rate' && L > 0 && R > 0) {
                        totalInput.value = (L * R).toFixed(2);
                    } else if (changedField === 'total' && T > 0 && R > 0) {
                        litersInput.value = (T / R).toFixed(2);
                    } else if (changedField === 'total' && T > 0 && L > 0) {
                        rateInput.value = (T / L).toFixed(2);
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
    """Server-side safeguard: if any 2 of (liters, rate, total) are provided,
    computes the 3rd accurately.
    """
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
            INSERT INTO fuel_logs (user_id, entry_date, fuel_type, odometer, fuel_liters, price_per_liter, total_cost, is_full_tank, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session["user_id"],
                entry_date,
                fuel_type,
                odometer,
                fuel_liters,
                price_per_liter,
                total_cost,
                is_full_tank,
                notes,
            ),
        )
        db.commit()
        flash("Refueling entry recorded.", "success")
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
            SET entry_date = ?, fuel_type = ?, odometer = ?, fuel_liters = ?,
                price_per_liter = ?, total_cost = ?, is_full_tank = ?, notes = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                entry_date,
                fuel_type,
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
        flash("Entry updated and mileage telemetry recalculated.", "success")
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