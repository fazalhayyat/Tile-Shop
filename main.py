"""
Tile Shop Inventory - Backend API
Run:  uvicorn main:app --host 0.0.0.0 --port 8000
Data: SQLite file tile_shop.db (auto-created, back this file up).
"""
import io
import json
import os
import sqlite3
import sys
import threading
import time
from typing import List

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

# ---------- paths / config ----------
USER_HOME = os.path.expanduser("~")
APP_DIR = os.path.join(USER_HOME, "Documents", "MianTiles")
os.makedirs(APP_DIR, exist_ok=True)
DB_PATH = os.getenv("TILE_DB", os.path.join(APP_DIR, "tile_shop.db"))
API_KEY = os.getenv("TILE_API_KEY", "")

app = FastAPI(title="Tile Shop Inventory API", version="1.1")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

# NOTE: invoice_items.product_id intentionally has no FK so a deleted product
# does not break historical invoices when the frontend pushes a full state.
SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    sku TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    category_id INTEGER REFERENCES categories(id),
    size TEXT DEFAULT '',
    finish TEXT DEFAULT '',
    supplier TEXT DEFAULT '',
    location TEXT DEFAULT '',
    sqft_per_box REAL DEFAULT 0,
    cost_price REAL DEFAULT 0,
    sell_price REAL DEFAULT 0,
    stock_boxes INTEGER DEFAULT 0,
    reorder_level INTEGER DEFAULT 5,
    created_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS movements (
    id INTEGER PRIMARY KEY,
    product_id INTEGER,
    kind TEXT NOT NULL CHECK (kind IN ('IN','OUT')),
    boxes INTEGER NOT NULL CHECK (boxes > 0),
    note TEXT DEFAULT '',
    created_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY,
    customer_name TEXT NOT NULL,
    contact TEXT DEFAULT '',
    address TEXT DEFAULT '',
    total_amount REAL DEFAULT 0,
    created_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS invoice_items (
    id INTEGER PRIMARY KEY,
    invoice_id INTEGER REFERENCES invoices(id),
    product_id INTEGER,
    product_name TEXT,
    qty INTEGER,
    price REAL,
    total REAL
);
"""

DEFAULT_CATEGORIES = ["Floor Tiles", "Wall Tiles", "Bathroom", "Kitchen",
                      "Outdoor", "Porcelain", "Ceramic", "Mosaic", "Accessories"]


def get_conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def init_db():
    with get_conn() as c:
        c.executescript(SCHEMA)
        if c.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0:
            c.executemany("INSERT INTO categories(id,name) VALUES (?,?)",
                          [(i + 1, n) for i, n in enumerate(DEFAULT_CATEGORIES)])


@app.middleware("http")
async def check_key(request: Request, call_next):
    if API_KEY and request.url.path.startswith("/api"):
        if request.headers.get("x-api-key") != API_KEY:
            return JSONResponse({"detail": "Invalid or missing API key"}, status_code=401)
    return await call_next(request)


# ---------------- models ----------------
class Category(BaseModel):
    id: int
    name: str

class Product(BaseModel):
    id: int
    sku: str
    name: str
    categoryId: int = 0
    size: str = ""
    finish: str = ""
    supplier: str = ""
    location: str = ""
    sqftPerBox: float = 0
    costPrice: float = 0
    sellPrice: float = 0
    stockBoxes: int = 0
    reorderLevel: int = 5
    createdAt: str = ""

class Movement(BaseModel):
    id: int
    productId: int
    kind: str
    boxes: int
    note: str = ""
    createdAt: str = ""

class InvoiceItem(BaseModel):
    id: int
    invoiceId: int
    productId: int
    productName: str
    qty: int
    price: float
    total: float

class Invoice(BaseModel):
    id: int
    customerName: str
    contact: str = ""
    address: str = ""
    totalAmount: float
    createdAt: str
    items: List[InvoiceItem] = []

class State(BaseModel):
    categories: List[Category] = []
    products: List[Product] = []
    movements: List[Movement] = []
    invoices: List[Invoice] = []


# ---------------- helpers ----------------
def rows_to_products(c):
    return [dict(
        id=r["id"], sku=r["sku"], name=r["name"], categoryId=r["category_id"] or 0,
        size=r["size"], finish=r["finish"], supplier=r["supplier"], location=r["location"],
        sqftPerBox=r["sqft_per_box"], costPrice=r["cost_price"], sellPrice=r["sell_price"],
        stockBoxes=r["stock_boxes"], reorderLevel=r["reorder_level"], createdAt=r["created_at"])
        for r in c.execute("SELECT * FROM products ORDER BY name")]


def rows_to_movements(c):
    return [dict(id=r["id"], productId=r["product_id"], kind=r["kind"],
                 boxes=r["boxes"], note=r["note"], createdAt=r["created_at"])
            for r in c.execute("SELECT * FROM movements ORDER BY id")]


def insert_product(c, p: Product):
    c.execute("""INSERT OR REPLACE INTO products
        (id,sku,name,category_id,size,finish,supplier,location,sqft_per_box,
         cost_price,sell_price,stock_boxes,reorder_level,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (p.id, p.sku, p.name, p.categoryId or None, p.size, p.finish, p.supplier,
         p.location, p.sqftPerBox, p.costPrice, p.sellPrice, p.stockBoxes,
         p.reorderLevel, p.createdAt))


# ---------------- state sync ----------------
@app.get("/api/health")
def health():
    return {"ok": True, "db": DB_PATH}


@app.get("/api/state")
def get_state():
    with get_conn() as c:
        invoices = []
        for inv in c.execute("SELECT * FROM invoices ORDER BY id DESC"):
            items = [dict(id=i["id"], invoiceId=i["invoice_id"], productId=i["product_id"],
                          productName=i["product_name"], qty=i["qty"], price=i["price"], total=i["total"])
                     for i in c.execute("SELECT * FROM invoice_items WHERE invoice_id=?", (inv["id"],))]
            invoices.append(dict(
                id=inv["id"], customerName=inv["customer_name"], contact=inv["contact"],
                address=inv["address"], totalAmount=inv["total_amount"], createdAt=inv["created_at"],
                items=items
            ))
        return {
            "categories": [dict(id=r["id"], name=r["name"]) for r in c.execute("SELECT * FROM categories ORDER BY name")],
            "products": rows_to_products(c),
            "movements": rows_to_movements(c),
            "invoices": invoices,
        }


@app.put("/api/state")
def put_state(state: State):
    # Temporarily disable FK enforcement so a full re-import cannot fail
    # because of historical invoice rows referencing a deleted product.
    conn = get_conn()
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DELETE FROM invoice_items")
        conn.execute("DELETE FROM invoices")
        conn.execute("DELETE FROM movements")
        conn.execute("DELETE FROM products")
        conn.execute("DELETE FROM categories")

        for cat in state.categories:
            conn.execute("INSERT OR REPLACE INTO categories(id,name) VALUES (?,?)", (cat.id, cat.name))
        for p in state.products:
            insert_product(conn, p)
        for m in state.movements:
            conn.execute("INSERT OR REPLACE INTO movements(id,product_id,kind,boxes,note,created_at) VALUES (?,?,?,?,?,?)",
                         (m.id, m.productId, m.kind, m.boxes, m.note, m.createdAt))
        for inv in state.invoices:
            conn.execute("INSERT INTO invoices(id,customer_name,contact,address,total_amount,created_at) VALUES (?,?,?,?,?,?)",
                         (inv.id, inv.customerName, inv.contact, inv.address, inv.totalAmount, inv.createdAt))
            for item in inv.items:
                conn.execute("INSERT INTO invoice_items(id,invoice_id,product_id,product_name,qty,price,total) VALUES (?,?,?,?,?,?,?)",
                             (item.id, inv.id, item.productId, item.productName, item.qty, item.price, item.total))
        conn.commit()
    finally:
        try:
            conn.execute("PRAGMA foreign_keys = ON")
        except Exception:
            pass
        conn.close()
    return {"ok": True}


# ---------------- Excel export (used by browser fallback) ----------------
@app.get("/api/export/excel")
def export_excel():
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "Inventory"

    headers = ["SKU", "Name", "Category", "Size", "Finish", "Supplier", "Location",
               "Sqft/box", "Cost Price", "Sell Price", "Boxes", "Reorder Level", "Status"]
    ws.append(headers)
    for col_num, _ in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_num)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill(start_color="1F6F78", end_color="1F6F78", fill_type="solid")

    with get_conn() as c:
        prods = rows_to_products(c)
        cats = [dict(id=r["id"], name=r["name"]) for r in c.execute("SELECT * FROM categories")]

    def cat_name(cid):
        for cat in cats:
            if cat['id'] == cid:
                return cat['name']
        return '—'

    for p in prods:
        status = "OK"
        if p["stockBoxes"] <= 0:
            status = "Out of stock"
        elif p["stockBoxes"] <= p["reorderLevel"]:
            status = "Low"
        ws.append([p["sku"], p["name"], cat_name(p["categoryId"]), p["size"], p["finish"],
                   p["supplier"], p["location"], p["sqftPerBox"], p["costPrice"],
                   p["sellPrice"], p["stockBoxes"], p["reorderLevel"], status])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=Mian_Tiles_Inventory.xlsx"},
    )


# ---------------- serve frontend ----------------
def get_resource_path(relative_path):
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), relative_path)


@app.get("/")
def read_index():
    return FileResponse(get_resource_path("index.html"), media_type="text/html")


@app.get("/image.jpeg")
def get_logo():
    return FileResponse(get_resource_path("image.jpeg"), media_type="image/jpeg")


# ---------------- Desktop API bridge ----------------
class Api:
    def download_excel(self):
        import webview
        import openpyxl  # ensure dependency present
        from openpyxl.styles import Font, PatternFill

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Inventory"
        headers = ["SKU", "Name", "Category", "Size", "Finish", "Supplier", "Location",
                   "Sqft/box", "Cost Price", "Sell Price", "Boxes", "Reorder Level", "Status"]
        ws.append(headers)
        for col_num, _ in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col_num)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill(start_color="1F6F78", end_color="1F6F78", fill_type="solid")

        with get_conn() as c:
            prods = rows_to_products(c)
            cats = [dict(id=r["id"], name=r["name"]) for r in c.execute("SELECT * FROM categories")]

        def cat_name(cid):
            for cat in cats:
                if cat['id'] == cid:
                    return cat['name']
            return '—'

        for p in prods:
            status = "OK"
            if p["stockBoxes"] <= 0:
                status = "Out of stock"
            elif p["stockBoxes"] <= p["reorderLevel"]:
                status = "Low"
            ws.append([p["sku"], p["name"], cat_name(p["categoryId"]), p["size"], p["finish"],
                       p["supplier"], p["location"], p["sqftPerBox"], p["costPrice"],
                       p["sellPrice"], p["stockBoxes"], p["reorderLevel"], status])

        window = webview.active_window()
        file_path = window.create_file_dialog(
            webview.SAVE_DIALOG,
            directory=os.path.expanduser("~\\Downloads"),
            save_filename='Mian_Tiles_Inventory.xlsx',
        )
        if file_path and len(file_path) > 0:
            try:
                wb.save(file_path[0])
                return {"status": "success"}
            except Exception as e:
                return {"status": "error", "message": str(e)}
        return {"status": "cancelled"}

    def backup_json(self):
        import webview
        state = get_state()
        window = webview.active_window()
        file_path = window.create_file_dialog(
            webview.SAVE_DIALOG,
            directory=os.path.expanduser("~\\Downloads"),
            save_filename='Mian_Tiles_Backup.json',
        )
        if file_path and len(file_path) > 0:
            try:
                with open(file_path[0], 'w', encoding='utf-8') as f:
                    json.dump(state, f, indent=2)
                return {"status": "success"}
            except Exception as e:
                return {"status": "error", "message": str(e)}
        return {"status": "cancelled"}

    def restore_json(self):
        import webview
        window = webview.active_window()
        file_path = window.create_file_dialog(
            webview.OPEN_DIALOG,
            directory=os.path.expanduser("~\\Downloads"),
            allow_multiple=False,
            file_types=('JSON Files (*.json)',),
        )
        if file_path and len(file_path) > 0:
            try:
                with open(file_path[0], 'r', encoding='utf-8') as f:
                    restored_data = json.load(f)
                return {"status": "success", "data": restored_data}
            except Exception as e:
                return {"status": "error", "message": str(e)}
        return {"status": "cancelled"}


# ---------------- desktop app startup ----------------
if __name__ == "__main__":
    import webview

    init_db()

    def start_server():
        uvicorn.run(app, host="127.0.0.1", port=8000, log_level="error")

    t = threading.Thread(target=start_server, daemon=True)
    t.start()
    time.sleep(1.5)

    api = Api()
    webview.create_window(
        title='Mian Tiles Inventory',
        url='http://127.0.0.1:8000',
        width=1200,
        height=900,
        js_api=api,
    )
    webview.start()