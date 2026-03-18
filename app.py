import os
import io
import sqlite3
from datetime import datetime, date, timedelta


from flask import (
    Flask,
    render_template_string,
    request,
    redirect,
    url_for,
    flash,
    Response,
    send_file,
)


# ============================================================
# App & DB Config
# ============================================================
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "cambia-esta-clave-segura")


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "pos.db")
REGALIA_CODE_DEFAULT = "SPOT2025"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    r = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?;", (table,)).fetchone()
    return r is not None


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    if not table_exists(conn, table): return False
    cols = conn.execute(f"PRAGMA table_info({table});").fetchall()
    return any(r["name"] == column for r in cols)


def ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl_fragment: str):
    if not column_exists(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl_fragment};")


def init_db():
    conn = get_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                category TEXT NOT NULL,
                sale_price INTEGER NOT NULL DEFAULT 0,
                cost_price INTEGER NOT NULL DEFAULT 0,
                stock INTEGER NOT NULL DEFAULT 0,
                image_blob BLOB,
                image_mime TEXT,
                created_at TEXT NOT NULL
            );
        """)
        conn.execute("CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT NOT NULL);")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sales (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                account_name TEXT NOT NULL,
                total INTEGER NOT NULL DEFAULT 0,
                cost_total INTEGER NOT NULL DEFAULT 0,
                profit INTEGER NOT NULL DEFAULT 0,
                is_gift INTEGER NOT NULL DEFAULT 0,
                cash_received INTEGER,
                change_given INTEGER,
                payment_method TEXT,
                status TEXT NOT NULL DEFAULT 'paid',
                closed_at TEXT
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sale_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sale_id INTEGER NOT NULL,
                product_id INTEGER,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                sale_price INTEGER NOT NULL,
                cost_price INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                delivered INTEGER NOT NULL DEFAULT 0,
                delivered_at TEXT,
                FOREIGN KEY(sale_id) REFERENCES sales(id)
            );
        """)
        ensure_column(conn, "sales", "payment_method", "payment_method TEXT")
        ensure_column(conn, "sales", "status", "status TEXT NOT NULL DEFAULT 'paid'")
        ensure_column(conn, "sale_items", "delivered", "delivered INTEGER NOT NULL DEFAULT 0")
        if conn.execute("SELECT v FROM settings WHERE k='regalia_code';").fetchone() is None:
            conn.execute("INSERT INTO settings(k,v) VALUES('regalia_code', ?);", (REGALIA_CODE_DEFAULT,))
        conn.commit()
    finally:
        conn.close()


# --- Funciones de Lógica ---
def adjust_stock(product_id: int, delta: int):
    conn = get_conn()
    try:
        row = conn.execute("SELECT stock FROM products WHERE id=?;", (product_id,)).fetchone()
        if not row: return False, "No existe"
        new_stock = row["stock"] + delta
        if new_stock < 0: return False, "Sin stock"
        conn.execute("UPDATE products SET stock=? WHERE id=?;", (new_stock, product_id))
        conn.commit()
        return True, ""
    finally: conn.close()


def get_product(product_id: int):
    conn = get_conn()
    try: return conn.execute("SELECT * FROM products WHERE id=?;", (product_id,)).fetchone()
    finally: conn.close()


# --- Cuentas en Memoria ---
cuentas = {"Cuenta General": {"items": []}}
cuenta_actual = "Cuenta General"


@app.route("/", methods=["GET"])
def index():
    conn = get_conn()
    prods = conn.execute("SELECT * FROM products ORDER BY category, name;").fetchall()
    conn.close()
    items = cuentas[cuenta_actual]["items"]
    total = sum(i["sale_price"] for i in items)
    return render_template_string(TEMPLATE_PRINCIPAL, cuentas=cuentas, cuenta_actual=cuenta_actual, products=prods, total=total)


@app.route("/agregar_item", methods=["POST"])
def agregar_item():
    pid = request.form.get("product_id", type=int)
    p = get_product(pid)
    if p and p["stock"] > 0:
        ok, _ = adjust_stock(pid, -1)
        if ok:
            cuentas[cuenta_actual]["items"].append({
                "product_id": p["id"], "name": p["name"], "category": p["category"],
                "sale_price": p["sale_price"], "cost_price": p["cost_price"], "delivered": 0
            })
    return redirect(url_for("index"))


@app.route("/cobrar", methods=["POST"])
def cobrar():
    global cuenta_actual
    items = cuentas[cuenta_actual]["items"]
    if not items: return redirect(url_for("index"))
    
    pay_method = request.form.get("payment_method", "efectivo")
    total = sum(i["sale_price"] for i in items)
    costo = sum(i["cost_price"] for i in items)
    
    conn = get_conn()
    try:
        now = datetime.now().isoformat()
        cur = conn.execute("INSERT INTO sales(created_at, account_name, total, cost_total, profit, payment_method, status) VALUES(?,?,?,?,?,?,?)",
                           (now, cuenta_actual, total, costo, total-costo, pay_method, "paid"))
        sale_id = cur.lastrowid
        for it in items:
            # IMPORTANTE: Aquí forzamos delivered=0 para que aparezca en Cocina
            conn.execute("INSERT INTO sale_items(sale_id, product_id, name, category, sale_price, cost_price, created_at, delivered) VALUES(?,?,?,?,?,?,?,0)",
                         (sale_id, it["product_id"], it["name"], it["category"], it["sale_price"], it["cost_price"], now))
        conn.commit()
    finally: conn.close()
    
    cuentas[cuenta_actual]["items"] = []
    if cuenta_actual != "Cuenta General":
        del cuentas[cuenta_actual]
        cuenta_actual = "Cuenta General"
    return redirect(url_for("index"))


# ============================================================
# Rutas de Cocina (Control de Despacho)
# ============================================================
@app.route("/cocina")
def cocina():
    conn = get_conn()
    try:
        # Traemos ítems no entregados de ventas que no estén cerradas
        rows = conn.execute("""
            SELECT si.id as sale_item_id, si.name, si.category, si.created_at as item_time,
                   s.id as sale_id, s.account_name, s.created_at as sale_time
            FROM sale_items si
            JOIN sales s ON s.id = si.sale_id
            WHERE si.delivered = 0 AND s.status != 'closed'
            ORDER BY s.created_at ASC
        """).fetchall()
    finally:
        conn.close()


    # Agrupamos por venta para armar los tickets
    grouped = {}
    for r in rows:
        sid = r["sale_id"]
        if sid not in grouped:
            grouped[sid] = {
                "sale_id": sid,
                "account_name": r["account_name"],
                "sale_created_at": r["sale_time"],
                "items": []
            }
        grouped[sid]["items"].append({
            "name": r["name"],
            "category": r["category"],
            "delivered": 0,
            "time": r["item_time"][11:16] # Solo HH:MM
        })
    
    # Convertimos a lista para el template
    sales_list = sorted(grouped.values(), key=lambda x: x["sale_created_at"])
    return render_template_string(TEMPLATE_COCINA, sales=sales_list, cuentas=grouped)


@app.route("/entregar_todo", methods=["POST"])
def entregar_todo():
    # El HTML del ticket envía el nombre de la cuenta
    nombre_cuenta = request.form.get("cuenta")
    if not nombre_cuenta:
        return redirect(url_for('cocina'))
        
    conn = get_conn()
    try:
        now = datetime.now().isoformat()
        # Buscamos la venta activa de esa cuenta
        sale = conn.execute("SELECT id FROM sales WHERE account_name=? AND status!='closed' ORDER BY created_at DESC", (nombre_cuenta,)).fetchone()
        if sale:
            sid = sale["id"]
            # Marcamos items como entregados y cerramos la venta
            conn.execute("UPDATE sale_items SET delivered=1, delivered_at=? WHERE sale_id=?", (now, sid))
            conn.execute("UPDATE sales SET status='closed', closed_at=? WHERE id=?", (now, sid))
            conn.commit()
    finally:
        conn.close()
    return redirect(url_for('cocina'))


# ============================================================
# Imágenes de Productos
# ============================================================
@app.route("/img/<int:product_id>")
def product_image(product_id: int):
    conn = get_conn()
    p = conn.execute("SELECT image_blob, image_mime FROM products WHERE id=?;", (product_id,)).fetchone()
    conn.close()
    if not p or not p["image_blob"]:
        return Response(b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;", mimetype="image/gif")
    return Response(p["image_blob"], mimetype=p["image_mime"] or "image/jpeg")


# ============================================================
# Ajustes / Inventario
# ============================================================
@app.route("/ajustes", methods=["GET", "POST"])
def ajustes():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add_product":
            name = request.form.get("name")
            cat = request.form.get("category", "comida").lower()
            price = request.form.get("sale_price", type=int)
            stock = request.form.get("stock", type=int)
            img = request.files.get("image")
            
            blob = img.read() if img else None
            mime = img.mimetype if img else None
            
            conn = get_conn()
            try:
                conn.execute("INSERT INTO products(name, category, sale_price, stock, image_blob, image_mime, created_at) VALUES(?,?,?,?,?,?,?)",
                             (name, cat, price, stock, blob, mime, datetime.now().isoformat()))
                conn.commit()
                flash("Producto agregado correctamente", "ok")
            except:
                flash("Error al agregar producto", "error")
            finally:
                conn.close()
            return redirect(url_for('ajustes'))


    conn = get_conn()
    prods = conn.execute("SELECT * FROM products ORDER BY category, name;").fetchall()
    conn.close()
    return render_template_string(TEMPLATE_AJUSTES, products=prods, regalia_code="********")


@app.route('/eliminar_producto_db/<int:product_id>', methods=['POST'])
def eliminar_producto_db(product_id):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
        conn.commit()
        flash("Producto eliminado", "ok")
    except Exception as e:
        flash(f"Error: {e}", "error")
    finally:
        conn.close()
    return redirect(url_for('ajustes'))


# ============================================================
# Reportes y Exportación
# ============================================================
@app.route("/reportes")
def reportes():
    conn = get_conn()
    try:
        sales = conn.execute("SELECT * FROM sales ORDER BY created_at DESC LIMIT 100").fetchall()
        totals = conn.execute("""
            SELECT COUNT(*) as n_ventas, 
                   SUM(total) as total_vendido, 
                   SUM(cost_total) as costo_total, 
                   SUM(profit) as ganancia_total,
                   SUM(CASE WHEN is_gift=1 THEN total ELSE 0 END) as total_regalias
            FROM sales
        """).fetchone()
    finally:
        conn.close()
    return render_template_string(TEMPLATE_REPORTES, sales=sales, totals=totals, daily=[], weekday=[], periodo="todo")


@app.route("/exportar")
def exportar():
    conn = get_conn()
    sales = conn.execute("SELECT * FROM sales ORDER BY created_at DESC").fetchall()
    conn.close()
    output = io.StringIO()
    output.write("ID,Fecha,Cuenta,Total,Costo,Ganancia,Metodo,Estado\n")
    for s in sales:
        output.write(f'{s["id"]},{s["created_at"]},{s["account_name"]},{s["total"]},{s["cost_total"]},{s["profit"]},{s["payment_method"]},{s["status"]}\n')
    mem = io.BytesIO(output.getvalue().encode("utf-8"))
    return send_file(mem, mimetype="text/csv", as_attachment=True, download_name="reporte_ventas.csv")


# ============================================================
# TEMPLATES (HTML)
# ============================================================


TEMPLATE_PRINCIPAL = """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <title>Sport Spot | POS Pro</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://unpkg.com/lucide@latest"></script>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;600;800&display=swap');
    body { font-family: 'Plus Jakarta Sans', sans-serif; background: radial-gradient(circle at top left, #166534 0%, #064e3b 40%, #020617 100%); min-height: 100vh; color: #e2e8f0; }
    .glass { background: rgba(255, 255, 255, 0.03); backdrop-filter: blur(12px); border: 1px solid rgba(255, 255, 255, 0.1); }
    .custom-scrollbar::-webkit-scrollbar { width: 6px; }
    .custom-scrollbar::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 10px; }
  </style>
</head>
<body class="p-4 lg:p-6">
<div class="max-w-[1600px] mx-auto">
  <header class="glass rounded-3xl p-5 mb-6 flex flex-col md:flex-row justify-between items-center gap-4">
    <div class="flex items-center gap-4">
      <div class="bg-green-500 p-3 rounded-2xl shadow-lg shadow-green-500/20"><i data-lucide="layout-dashboard" class="text-white w-6 h-6"></i></div>
      <div><h1 class="text-xl font-extrabold text-white">SPORT SPOT <span class="text-green-400">POS</span></h1><p class="text-xs text-slate-400 uppercase tracking-widest">Gestión e Inventarios</p></div>
    </div>
    <nav class="flex gap-2">
      <a href="/cocina" class="flex items-center gap-2 px-4 py-2 rounded-xl glass hover:bg-white/10 text-sm font-semibold"><i data-lucide="utensils" class="w-4 h-4 text-orange-400"></i> Cocina</a>
      <a href="/reportes" class="flex items-center gap-2 px-4 py-2 rounded-xl glass hover:bg-white/10 text-sm font-semibold"><i data-lucide="bar-chart-3" class="w-4 h-4 text-blue-400"></i> Reportes</a>
      <a href="/ajustes" class="flex items-center gap-2 px-4 py-2 rounded-xl glass hover:bg-white/10 text-sm font-semibold"><i data-lucide="settings" class="w-4 h-4 text-slate-400"></i> Ajustes</a>
    </nav>
  </header>
  <div class="grid grid-cols-1 lg:grid-cols-12 gap-6">
    <main class="lg:col-span-8">
      <div class="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-4 gap-4 overflow-y-auto max-h-[75vh] pr-2 custom-scrollbar">
        {% for p in products %}
        <div class="glass rounded-2xl p-3 group hover:border-green-500/50 transition-all duration-300">
          <div class="relative h-32 rounded-xl overflow-hidden mb-3 bg-black/20">
            <img src="/img/{{p.id}}" class="w-full h-full object-cover group-hover:scale-110 transition-transform">
            <div class="absolute top-2 right-2 bg-black/60 px-2 py-1 rounded-lg text-[10px] font-bold border border-white/10">STOCK: {{p.stock}}</div>
          </div>
          <div class="flex justify-between items-start mb-2">
            <div><h3 class="font-bold text-sm leading-tight">{{p.name}}</h3><span class="text-[10px] uppercase font-bold text-green-400">{{p.category}}</span></div>
            <span class="font-extrabold text-sm text-white">₡{{p.sale_price}}</span>
          </div>
          <form method="post" action="/agregar_item">
            <input type="hidden" name="product_id" value="{{p.id}}">
            <button type="submit" class="w-full py-2 rounded-xl bg-green-500 hover:bg-green-400 text-green-950 font-bold text-xs transition-colors shadow-lg active:scale-95">AGREGAR</button>
          </form>
        </div>
        {% endfor %}
      </div>
    </main>
    <aside class="lg:col-span-4 space-y-6">
      <div class="glass rounded-3xl p-6 shadow-2xl sticky top-6">
        <h2 class="text-lg font-bold mb-4 flex items-center gap-2"><i data-lucide="receipt" class="text-blue-400"></i> Cuenta Actual</h2>
        <div class="bg-black/20 rounded-2xl p-2 mb-6 min-h-[150px] max-h-[300px] overflow-y-auto custom-scrollbar">
          {% for item in cuentas[cuenta_actual]['items'] %}
          <div class="flex justify-between items-center p-3 rounded-xl hover:bg-white/5 transition-all">
            <div class="flex-1"><p class="text-sm font-semibold">{{item.name}}</p><p class="text-[10px] text-amber-500 font-bold uppercase tracking-tighter">Pendiente</p></div>
            <span class="text-sm font-bold text-white">₡{{item.sale_price}}</span>
          </div>
          {% endfor %}
        </div>
        <div class="border-t border-white/10 pt-4 space-y-4">
          <div class="flex justify-between items-end"><span class="text-xs font-bold text-slate-400 uppercase tracking-widest">Total a pagar</span><span class="text-3xl font-black text-white">₡{{total}}</span></div>
          <form method="post" action="/cobrar" class="space-y-3">
            <select name="payment_method" class="w-full bg-black/20 border border-white/10 rounded-xl px-3 py-2 text-xs text-white outline-none focus:border-green-500">
              <option value="efectivo">Efectivo</option><option value="tarjeta">Tarjeta</option><option value="sinpe">SINPE</option>
            </select>
            <input name="cash_received" class="w-full bg-black/20 border border-white/10 rounded-xl px-3 py-2 text-xs text-white outline-none focus:border-green-500" placeholder="Paga con (ej: 5000)">
            <button type="submit" class="w-full py-4 bg-green-500 hover:bg-green-400 text-green-950 font-black rounded-2xl shadow-xl transition-all active:scale-[0.98] flex items-center justify-center gap-2">
              <i data-lucide="banknote" class="w-5 h-5"></i> FINALIZAR VENTA
            </button>
          </form>
        </div>
      </div>
    </aside>
  </div>
</div>
<script>lucide.createIcons();</script>
</body>
</html>
"""


TEMPLATE_COCINA = """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8"><title>Cocina | Sport Spot</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <script src="https://cdn.tailwindcss.com"></script><script src="https://unpkg.com/lucide@latest"></script>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Plus+Jakarta+Sans:wght@600;800&display=swap');
    body { font-family: 'Plus Jakarta Sans', sans-serif; background: #0f172a; background-image: radial-gradient(#1e293b 1px, transparent 1px); background-size: 20px 20px; min-height: 100vh; }
    .ticket { font-family: 'Space Mono', monospace; background: #fff; color: #1a1a1a; position: relative; clip-path: polygon(0% 0%, 100% 0%, 100% 95%, 95% 100%, 85% 95%, 75% 100%, 65% 95%, 55% 100%, 45% 95%, 35% 100%, 25% 95%, 15% 100%, 5% 95%, 0% 100%); }
  </style>
</head>
<body class="text-slate-200">
  <nav class="bg-slate-900/80 backdrop-blur-md p-4 mb-8 border-b border-white/10">
    <div class="max-w-7xl mx-auto flex justify-between items-center">
      <div class="flex items-center gap-3"><div class="bg-orange-500 p-2 rounded-xl shadow-lg"><i data-lucide="flame" class="text-white w-6 h-6 animate-pulse"></i></div><h1 class="text-xl font-black uppercase tracking-tighter">Cocina</h1></div>
      <a href="/" class="px-4 py-2 rounded-xl bg-white/5 hover:bg-white/10 text-sm font-bold border border-white/10">Volver al POS</a>
    </div>
  </nav>
  <main class="max-w-7xl mx-auto px-4 grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-8">
    {% for sid, data in cuentas.items() %}
    <div class="flex flex-col gap-2">
      <div class="ticket p-5 shadow-2xl rounded-t-sm">
        <div class="border-b-2 border-dashed border-slate-300 pb-3 mb-4">
          <span class="text-[10px] font-bold text-slate-400 uppercase tracking-widest">Cuenta</span>
          <h2 class="text-xl font-black text-slate-900 leading-none">{{ data.account_name }}</h2>
        </div>
        <div class="space-y-3 mb-6">
          {% for item in data.items %}
          <div class="flex gap-2"><span class="font-bold text-slate-400">1x</span><p class="text-sm font-bold text-slate-800 leading-tight uppercase">{{ item.name }}</p></div>
          {% endfor %}
        </div>
        <form method="post" action="/entregar_todo">
          <input type="hidden" name="cuenta" value="{{ data.account_name }}">
          <button type="submit" class="w-full py-3 bg-slate-900 hover:bg-green-600 text-white font-black text-xs rounded-xl transition-all flex items-center justify-center gap-2">
            <i data-lucide="check-circle-2" class="w-4 h-4"></i> ORDEN LISTA
          </button>
        </form>
      </div>
    </div>
    {% endfor %}
  </main>
  <script>lucide.createIcons(); setTimeout(() => window.location.reload(), 30000);</script>
</body>
</html>
"""


TEMPLATE_AJUSTES = """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8"><title>Ajustes | Sport Spot</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <script src="https://cdn.tailwindcss.com"></script><script src="https://unpkg.com/lucide@latest"></script>
  <style> body { font-family: 'Plus Jakarta Sans', sans-serif; background: radial-gradient(circle at top left, #1e293b 0%, #0f172a 100%); min-height: 100vh; color: #e2e8f0; } .glass { background: rgba(255, 255, 255, 0.03); backdrop-filter: blur(12px); border: 1px solid rgba(255, 255, 255, 0.1); } </style>
</head>
<body class="p-4 lg:p-8">
  <div class="max-w-5xl mx-auto">
    <header class="flex justify-between items-center mb-8"><div class="flex items-center gap-4"><a href="/" class="p-3 glass rounded-2xl text-blue-400"><i data-lucide="arrow-left" class="w-6 h-6"></i></a><h1 class="text-2xl font-extrabold">Configuración</h1></div></header>
    <div class="grid grid-cols-1 md:grid-cols-2 gap-8">
      <section class="glass rounded-3xl p-6"><h2 class="text-lg font-bold mb-6 flex items-center gap-3"><i data-lucide="plus-circle" class="text-blue-400"></i> Nuevo Producto</h2>
        <form method="post" action="/ajustes" enctype="multipart/form-data" class="space-y-4">
          <input type="hidden" name="action" value="add_product">
          <input name="name" placeholder="Nombre" required class="w-full bg-black/20 border border-white/10 rounded-xl px-4 py-3 text-sm focus:border-blue-500 outline-none transition-all">
          <div class="grid grid-cols-2 gap-4">
            <input type="number" name="sale_price" placeholder="Precio ₡" required class="w-full bg-black/20 border border-white/10 rounded-xl px-4 py-3 text-sm focus:border-blue-500 outline-none transition-all">
            <input type="number" name="stock" placeholder="Stock" required class="w-full bg-black/20 border border-white/10 rounded-xl px-4 py-3 text-sm focus:border-blue-500 outline-none transition-all">
          </div>
          <button type="submit" class="w-full py-4 bg-blue-600 hover:bg-blue-500 text-white font-black rounded-2xl shadow-lg shadow-blue-500/20 active:scale-95 transition-all">GUARDAR PRODUCTO</button>
        </form>
      </section>
      <section class="glass rounded-3xl p-6 overflow-hidden flex flex-col"><h2 class="text-lg font-bold mb-6 flex items-center gap-3"><i data-lucide="package" class="text-amber-400"></i> Inventario</h2>
        <div class="space-y-3 overflow-y-auto max-h-[400px] pr-2">
          {% for p in products %}
          <div class="flex justify-between items-center p-3 rounded-2xl bg-white/5 border border-white/5">
            <div class="flex items-center gap-3"><img src="/img/{{p.id}}" class="w-10 h-10 rounded-lg object-cover"><div><p class="text-sm font-bold">{{p.name}}</p><p class="text-[10px] text-slate-500 font-bold uppercase">{{p.category}}</p></div></div>
            <div class="flex items-center gap-4">
              <span class="text-sm font-black text-green-400">{{p.stock}}</span>
              <form method="post" action="/eliminar_producto_db/{{p.id}}"><button type="submit" class="p-2 text-slate-600 hover:text-rose-500 transition-colors"><i data-lucide="trash-2" class="w-4 h-4"></i></button></form>
            </div>
          </div>
          {% endfor %}
        </div>
      </section>
    </div>
  </div>
  <script>lucide.createIcons();</script>
</body>
</html>
"""


TEMPLATE_REPORTES = """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8"><title>Reportes | Sport Spot</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <script src="https://cdn.tailwindcss.com"></script><script src="https://unpkg.com/lucide@latest"></script>
  <style> body { font-family: 'Plus Jakarta Sans', sans-serif; background: #0f172a; color: #e2e8f0; } .glass { background: rgba(255, 255, 255, 0.03); backdrop-filter: blur(12px); border: 1px solid rgba(255, 255, 255, 0.1); } </style>
</head>
<body class="p-4 lg:p-8">
  <div class="max-w-6xl mx-auto">
    <header class="flex justify-between items-center mb-8"><div class="flex gap-4"><a href="/" class="p-3 glass rounded-2xl text-green-400"><i data-lucide="arrow-left" class="w-6 h-6"></i></a><h1 class="text-2xl font-extrabold">Reportes</h1></div><a href="/exportar" class="bg-blue-600 px-6 py-3 rounded-xl font-bold hover:bg-blue-500 transition-all">Exportar CSV</a></header>
    <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
      <div class="glass p-6 rounded-3xl text-center"><p class="text-slate-400 text-xs font-bold uppercase mb-2">Ventas</p><p class="text-2xl font-black text-white">{{ totals.n_ventas }}</p></div>
      <div class="glass p-6 rounded-3xl text-center"><p class="text-slate-400 text-xs font-bold uppercase mb-2">Total</p><p class="text-2xl font-black text-green-400">₡{{ totals.total_vendido }}</p></div>
      <div class="glass p-6 rounded-3xl text-center"><p class="text-slate-400 text-xs font-bold uppercase mb-2">Ganancia</p><p class="text-2xl font-black text-blue-400">₡{{ totals.ganancia_total }}</p></div>
      <div class="glass p-6 rounded-3xl text-center"><p class="text-slate-400 text-xs font-bold uppercase mb-2">Regalías</p><p class="text-2xl font-black text-amber-400">₡{{ totals.total_regalias }}</p></div>
    </div>
    <div class="glass rounded-3xl p-6 overflow-hidden">
      <table class="w-full text-left text-sm">
        <thead class="bg-white/5"><tr class="text-slate-400 font-bold uppercase text-xs border-b border-white/10"><th class="p-4">Fecha</th><th class="p-4">Cuenta</th><th class="p-4">Total</th><th class="p-4">Pago</th></tr></thead>
        <tbody>
          {% for s in sales %}
          <tr class="border-b border-white/5 hover:bg-white/5 transition-all"><td class="p-4">{{ s.created_at[:10] }}</td><td class="p-4 font-bold">{{ s.account_name }}</td><td class="p-4 text-green-400 font-black">₡{{ s.total }}</td><td class="p-4 uppercase text-xs font-bold">{{ s.payment_method }}</td></tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
  <script>lucide.createIcons();</script>
</body>
</html>
"""


# ============================================================
# Boot
# ============================================================
init_db()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)






