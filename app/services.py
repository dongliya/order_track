from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from math import ceil
import shutil
from pathlib import Path
from uuid import uuid4

from fastapi import Request, UploadFile
from sqlalchemy import func, inspect, select, text
from sqlalchemy.orm import Session, joinedload
from werkzeug.security import check_password_hash, generate_password_hash

from app.db import Base, engine
from app.models import Invoice, OperationLog, Order, Payment, Product, ProductCatalog, Shipment, SystemLog, User

UPLOAD_ROOT = Path(__file__).resolve().parent.parent / "uploads"
PRODUCT_UPLOAD_DIR = UPLOAD_ROOT / "products"
LEGACY_CONTRACT_UPLOAD_DIR = UPLOAD_ROOT / "contracts"
ORDER_CONTRACT_UPLOAD_DIR = UPLOAD_ROOT / "order_contracts"


def init_db() -> None:
    run_migrations()
    PRODUCT_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    ORDER_CONTRACT_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)


def run_migrations() -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    if "contracts" in tables and "products" not in tables:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE contracts RENAME TO products"))
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())

    if "products" in tables:
        product_columns = {column["name"] for column in inspector.get_columns("products")}
        statements: list[str] = []

        rename_pairs = [
            ("contract_no", "product_no"),
            ("contract_name", "product_name"),
            ("contract_amount", "product_amount"),
        ]
        for old_name, new_name in rename_pairs:
            if old_name in product_columns and new_name not in product_columns:
                statements.append(f"ALTER TABLE products RENAME COLUMN {old_name} TO {new_name}")

        if "unit_price" not in product_columns:
            statements.append("ALTER TABLE products ADD COLUMN unit_price NUMERIC(18, 2)")
        if "quantity" not in product_columns:
            statements.append("ALTER TABLE products ADD COLUMN quantity NUMERIC(18, 2)")
        if "catalog_id" not in product_columns:
            statements.append("ALTER TABLE products ADD COLUMN catalog_id INTEGER")

        if statements:
            with engine.begin() as connection:
                for statement in statements:
                    connection.execute(text(statement))

        _ensure_product_catalog_table()
        refreshed_product_columns = {column["name"] for column in inspect(engine).get_columns("products")}
        if "sign_date" in refreshed_product_columns:
            _rebuild_products_table()
        _migrate_product_catalogs()
        _migrate_product_uploads()

    if "orders" in tables:
        order_columns = {column["name"] for column in inspector.get_columns("orders")}
        order_statements: list[str] = []

        if "contract_status" in order_columns and "product_status" not in order_columns:
            order_statements.append("ALTER TABLE orders RENAME COLUMN contract_status TO product_status")

        if "operator_name" not in order_columns:
            order_statements.append("ALTER TABLE orders ADD COLUMN operator_name VARCHAR(100)")
        if "contract_file_path" not in order_columns:
            order_statements.append("ALTER TABLE orders ADD COLUMN contract_file_path VARCHAR(255)")
        if "contract_original_filename" not in order_columns:
            order_statements.append("ALTER TABLE orders ADD COLUMN contract_original_filename VARCHAR(255)")

        if order_statements:
            with engine.begin() as connection:
                for statement in order_statements:
                    connection.execute(text(statement))
                if "owner_name" in order_columns:
                    connection.execute(
                        text(
                            """
                            UPDATE orders
                            SET operator_name = COALESCE(operator_name, owner_name)
                            WHERE operator_name IS NULL OR operator_name = ''
                            """
                        )
                    )

        refreshed_columns = {column["name"] for column in inspect(engine).get_columns("orders")}
        if "product_status" in refreshed_columns:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        UPDATE orders
                        SET product_status =
                            CASE
                                WHEN product_status = '已上传' THEN '已录入'
                                WHEN product_status = '未上传' THEN '未录入'
                                ELSE product_status
                            END
                        """
                    )
                )
                if "products" in tables:
                    connection.execute(
                        text(
                            """
                            UPDATE orders
                            SET product_status =
                                CASE
                                    WHEN EXISTS (
                                        SELECT 1
                                        FROM products
                                        WHERE products.order_id = orders.id
                                    ) THEN '已录入'
                                    ELSE '未录入'
                                END
                            """
                    )
                )

    if "operation_logs" not in tables:
        Base.metadata.create_all(bind=engine, tables=[OperationLog.__table__])
    if "system_logs" not in tables:
        Base.metadata.create_all(bind=engine, tables=[SystemLog.__table__])

    if "shipments" in tables:
        existing_columns = {column["name"] for column in inspector.get_columns("shipments")}
        statements: list[str] = []

        if "shipment_status" not in existing_columns:
            statements.append("ALTER TABLE shipments ADD COLUMN shipment_status VARCHAR(20) DEFAULT '未发货'")
        if "receiver_info" not in existing_columns:
            statements.append("ALTER TABLE shipments ADD COLUMN receiver_info TEXT")

        if statements:
            with engine.begin() as connection:
                for statement in statements:
                    connection.execute(text(statement))
                if "shipment_status" not in existing_columns:
                    if "shipment_amount" in existing_columns:
                        connection.execute(
                            text(
                                """
                                UPDATE shipments
                                SET shipment_status =
                                    CASE
                                        WHEN COALESCE(shipment_amount, 0) <= 0 THEN '未发货'
                                        ELSE '已发货'
                                    END
                                WHERE shipment_status IS NULL OR shipment_status = ''
                                """
                            )
                        )
                    else:
                        connection.execute(
                            text(
                                """
                                UPDATE shipments
                                SET shipment_status = '已发货'
                                WHERE shipment_status IS NULL OR shipment_status = ''
                                """
                            )
                        )
                if "receiver_info" not in existing_columns:
                    connection.execute(
                        text(
                            """
                            UPDATE shipments
                            SET receiver_info = TRIM(
                                COALESCE(receiver_name, '') ||
                                CASE WHEN receiver_phone IS NOT NULL AND receiver_phone != '' THEN ' / ' || receiver_phone ELSE '' END ||
                                CASE WHEN receiver_address IS NOT NULL AND receiver_address != '' THEN ' / ' || receiver_address ELSE '' END
                            )
                            WHERE receiver_info IS NULL OR receiver_info = ''
                            """
                        )
                    )

        refreshed_shipment_columns = {column["name"] for column in inspect(engine).get_columns("shipments")}
        if "shipment_amount" in refreshed_shipment_columns:
            _rebuild_shipments_table()


def ensure_seed_data(db: Session) -> None:
    admin = db.scalar(select(User).where(User.username == "admin"))
    if admin is None:
        db.add(
            User(
                username="admin",
                real_name="系统管理员",
                role="admin",
                is_active=True,
                password_hash=generate_password_hash("admin123"),
            )
        )
        db.commit()


def authenticate(db: Session, username: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.username == username))
    if user is None or not user.is_active:
        return None
    if not check_password_hash(user.password_hash, password):
        return None
    return user


def save_login(request: Request, user: User) -> None:
    request.session["user_id"] = user.id


def logout(request: Request) -> None:
    request.session.pop("user_id", None)


def get_current_user(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.get(User, user_id)


def set_flash(request: Request, message: str, level: str = "info") -> None:
    request.session["flash"] = {"message": message, "level": level}


def pop_flash(request: Request) -> dict | None:
    return request.session.pop("flash", None)


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


def parse_decimal(value: str | None) -> Decimal:
    if not value:
        return Decimal("0.00")
    try:
        return Decimal(value).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return Decimal("0.00")


def decimal_or_none(value: str | None) -> Decimal | None:
    if not value:
        return None
    return parse_decimal(value)


def integer_decimal_or_none(value: str | None) -> Decimal | None:
    if not value:
        return None
    try:
        return Decimal(int(Decimal(value)))
    except (InvalidOperation, ValueError):
        return None


def calculate_product_amount(unit_price: Decimal | None, quantity: Decimal | None, amount: Decimal | None) -> Decimal | None:
    if amount is not None:
        return amount
    if unit_price is None or quantity is None:
        return None
    return (unit_price * quantity).quantize(Decimal("0.01"))


def validate_amount_not_exceed_order(order: Order, amount: Decimal, field_label: str) -> None:
    if amount > order.order_amount:
        raise ValueError(f"{field_label}不能大于订单金额")


def product_upload_path(order_id: int, filename: str) -> Path:
    suffix = Path(filename).suffix
    relative = Path(str(order_id)) / f"{uuid4().hex}{suffix}"
    return PRODUCT_UPLOAD_DIR / relative


def order_contract_upload_path(order_id: int, filename: str) -> Path:
    suffix = Path(filename).suffix
    relative = Path(str(order_id)) / f"{uuid4().hex}{suffix}"
    return ORDER_CONTRACT_UPLOAD_DIR / relative


def _migrate_product_uploads() -> None:
    if not LEGACY_CONTRACT_UPLOAD_DIR.exists():
        return
    PRODUCT_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    for legacy_file in LEGACY_CONTRACT_UPLOAD_DIR.iterdir():
        if not legacy_file.is_file():
            continue
        target = PRODUCT_UPLOAD_DIR / legacy_file.name
        if target.exists():
            continue
        shutil.move(str(legacy_file), str(target))


def _ensure_product_catalog_table() -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS product_catalog (
                    id INTEGER NOT NULL PRIMARY KEY,
                    product_no VARCHAR(100),
                    product_name VARCHAR(150) NOT NULL UNIQUE,
                    unit_price NUMERIC(18, 2),
                    remark TEXT,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )


def _migrate_product_catalogs() -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO product_catalog (product_no, product_name, unit_price, remark, created_at, updated_at)
                SELECT MIN(products.product_no),
                       products.product_name,
                       MIN(products.unit_price),
                       NULL,
                       MIN(products.created_at),
                       MAX(products.updated_at)
                FROM products
                WHERE TRIM(COALESCE(products.product_name, '')) != ''
                  AND NOT EXISTS (
                      SELECT 1
                      FROM product_catalog
                      WHERE product_catalog.product_name = products.product_name
                  )
                GROUP BY products.product_name
                """
            )
        )
        connection.execute(
            text(
                """
                UPDATE products
                SET catalog_id = (
                    SELECT product_catalog.id
                    FROM product_catalog
                    WHERE product_catalog.product_name = products.product_name
                    LIMIT 1
                )
                WHERE catalog_id IS NULL
                """
            )
        )


def _rebuild_products_table() -> None:
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS products__new"))
        connection.execute(
            text(
                """
                CREATE TABLE products__new (
                    id INTEGER NOT NULL PRIMARY KEY,
                    order_id INTEGER NOT NULL,
                    catalog_id INTEGER,
                    product_no VARCHAR(100),
                    product_name VARCHAR(150) NOT NULL,
                    unit_price NUMERIC(18, 2),
                    quantity NUMERIC(18, 2),
                    product_amount NUMERIC(18, 2),
                    file_path VARCHAR(255),
                    original_filename VARCHAR(255),
                    remark TEXT,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    FOREIGN KEY(order_id) REFERENCES orders (id),
                    FOREIGN KEY(catalog_id) REFERENCES product_catalog (id)
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO products__new (
                    id,
                    order_id,
                    catalog_id,
                    product_no,
                    product_name,
                    unit_price,
                    quantity,
                    product_amount,
                    file_path,
                    original_filename,
                    remark,
                    created_at,
                    updated_at
                )
                SELECT
                    id,
                    order_id,
                    catalog_id,
                    product_no,
                    product_name,
                    unit_price,
                    quantity,
                    product_amount,
                    file_path,
                    original_filename,
                    remark,
                    created_at,
                    updated_at
                FROM products
                """
            )
        )
        connection.execute(text("DROP TABLE products"))
        connection.execute(text("ALTER TABLE products__new RENAME TO products"))


def _rebuild_shipments_table() -> None:
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS shipments__new"))
        connection.execute(
            text(
                """
                CREATE TABLE shipments__new (
                    id INTEGER NOT NULL PRIMARY KEY,
                    order_id INTEGER NOT NULL,
                    shipment_date DATE,
                    shipment_batch VARCHAR(100),
                    shipment_status VARCHAR(20) NOT NULL,
                    shipment_content TEXT,
                    receiver_info TEXT,
                    receiver_name VARCHAR(100),
                    receiver_phone VARCHAR(50),
                    receiver_address VARCHAR(255),
                    logistics_company VARCHAR(100),
                    tracking_no VARCHAR(100),
                    remark TEXT,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    FOREIGN KEY(order_id) REFERENCES orders (id)
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO shipments__new (
                    id,
                    order_id,
                    shipment_date,
                    shipment_batch,
                    shipment_status,
                    shipment_content,
                    receiver_info,
                    receiver_name,
                    receiver_phone,
                    receiver_address,
                    logistics_company,
                    tracking_no,
                    remark,
                    created_at,
                    updated_at
                )
                SELECT
                    id,
                    order_id,
                    shipment_date,
                    shipment_batch,
                    shipment_status,
                    shipment_content,
                    receiver_info,
                    receiver_name,
                    receiver_phone,
                    receiver_address,
                    logistics_company,
                    tracking_no,
                    remark,
                    created_at,
                    updated_at
                FROM shipments
                """
            )
        )
        connection.execute(text("DROP TABLE shipments"))
        connection.execute(text("ALTER TABLE shipments__new RENAME TO shipments"))


def recalculate_order(order: Order) -> None:
    order.order_amount = sum(
        (item.product_amount or Decimal("0.00") for item in order.products),
        Decimal("0.00"),
    )
    order.shipped_amount = Decimal("0.00")
    order.paid_amount = sum((item.payment_amount or Decimal("0.00") for item in order.payments), Decimal("0.00"))
    order.invoice_amount = sum((item.invoice_amount or Decimal("0.00") for item in order.invoices), Decimal("0.00"))
    order.product_status = "已录入" if order.products else "未录入"
    order.shipment_status = summarize_shipment_status(order.shipments)
    order.payment_status = calc_progress_status(order.paid_amount, order.order_amount, "未付款", "部分付款", "已付款")
    order.invoice_status = calc_progress_status(order.invoice_amount, order.order_amount, "未开票", "部分开票", "已开票")


def calc_progress_status(current: Decimal, total: Decimal, empty: str, partial: str, done: str) -> str:
    if current <= 0:
        return empty
    if total > 0 and current >= total:
        return done
    return partial


def summarize_shipment_status(shipments: list[Shipment]) -> str:
    if not shipments:
        return "未发货"

    statuses = {item.shipment_status or "未发货" for item in shipments}
    if statuses == {"未发货"}:
        return "未发货"
    if statuses == {"已发货"}:
        return "已发货"
    return "部分发货"


def order_query(db: Session):
    return (
        select(Order)
        .where(Order.is_deleted.is_(False))
        .options(
            joinedload(Order.products).joinedload(Product.catalog),
            joinedload(Order.shipments),
            joinedload(Order.payments),
            joinedload(Order.invoices),
            joinedload(Order.operation_logs),
        )
        .order_by(Order.created_at.desc())
    )


def deleted_order_query(db: Session):
    return (
        select(Order)
        .where(Order.is_deleted.is_(True))
        .options(
            joinedload(Order.products).joinedload(Product.catalog),
            joinedload(Order.shipments),
            joinedload(Order.payments),
            joinedload(Order.invoices),
            joinedload(Order.operation_logs),
        )
        .order_by(Order.updated_at.desc(), Order.created_at.desc())
    )


def apply_order_filters(query, filters: dict[str, str]):
    if filters.get("order_no"):
        query = query.where(Order.order_no.contains(filters["order_no"]))
    if filters.get("customer_name"):
        query = query.where(Order.customer_name.contains(filters["customer_name"]))
    if filters.get("project_name"):
        query = query.where(Order.project_name.contains(filters["project_name"]))
    if filters.get("product_status"):
        query = query.where(Order.product_status == filters["product_status"])
    if filters.get("shipment_status"):
        query = query.where(Order.shipment_status == filters["shipment_status"])
    if filters.get("payment_status"):
        query = query.where(Order.payment_status == filters["payment_status"])
    if filters.get("invoice_status"):
        query = query.where(Order.invoice_status == filters["invoice_status"])
    if filters.get("order_status"):
        query = query.where(Order.order_status == filters["order_status"])
    if filters.get("date_from"):
        query = query.where(Order.order_date >= parse_date(filters["date_from"]))
    if filters.get("date_to"):
        query = query.where(Order.order_date <= parse_date(filters["date_to"]))
    return query


def list_orders(db: Session, filters: dict[str, str]) -> list[Order]:
    query = apply_order_filters(order_query(db), filters)
    return db.scalars(query).unique().all()


def paginate_orders(db: Session, filters: dict[str, str], page: int, page_size: int = 10) -> dict:
    page = max(page, 1)
    page_size = max(page_size, 1)

    count_query = apply_order_filters(
        select(func.count()).select_from(Order).where(Order.is_deleted.is_(False)),
        filters,
    )
    total_count = db.scalar(count_query) or 0
    total_pages = max(ceil(total_count / page_size), 1)
    page = min(page, total_pages)
    offset = (page - 1) * page_size

    orders_query = apply_order_filters(order_query(db), filters).offset(offset).limit(page_size)
    orders = db.scalars(orders_query).unique().all()

    start_page = max(page - 2, 1)
    end_page = min(start_page + 4, total_pages)
    start_page = max(end_page - 4, 1)

    return {
        "orders": orders,
        "page": page,
        "page_size": page_size,
        "total_count": total_count,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "page_numbers": list(range(start_page, end_page + 1)),
    }


def paginate_deleted_orders(db: Session, filters: dict[str, str], page: int, page_size: int = 10) -> dict:
    page = max(page, 1)
    page_size = max(page_size, 1)

    count_query = apply_order_filters(
        select(func.count()).select_from(Order).where(Order.is_deleted.is_(True)),
        filters,
    )
    total_count = db.scalar(count_query) or 0
    total_pages = max(ceil(total_count / page_size), 1)
    page = min(page, total_pages)
    offset = (page - 1) * page_size

    orders_query = apply_order_filters(deleted_order_query(db), filters).offset(offset).limit(page_size)
    orders = db.scalars(orders_query).unique().all()

    start_page = max(page - 2, 1)
    end_page = min(start_page + 4, total_pages)
    start_page = max(end_page - 4, 1)

    return {
        "orders": orders,
        "page": page,
        "page_size": page_size,
        "total_count": total_count,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "page_numbers": list(range(start_page, end_page + 1)),
    }


def system_log_query():
    return (
        select(SystemLog)
        .order_by(SystemLog.created_at.desc())
    )


def apply_system_log_filters(query, filters: dict[str, str]):
    if filters.get("order_no"):
        query = query.where(SystemLog.order_no.contains(filters["order_no"]))
    if filters.get("operator_name"):
        query = query.where(SystemLog.operator_name.contains(filters["operator_name"]))
    if filters.get("module_name"):
        query = query.where(SystemLog.module_name == filters["module_name"])
    if filters.get("keyword"):
        keyword = filters["keyword"]
        query = query.where(SystemLog.content.contains(keyword))
    return query


def paginate_system_logs(db: Session, filters: dict[str, str], page: int, page_size: int = 20) -> dict:
    page = max(page, 1)
    page_size = max(page_size, 1)

    count_query = apply_system_log_filters(
        select(func.count()).select_from(SystemLog),
        filters,
    )
    total_count = db.scalar(count_query) or 0
    total_pages = max(ceil(total_count / page_size), 1)
    page = min(page, total_pages)
    offset = (page - 1) * page_size

    logs_query = apply_system_log_filters(system_log_query(), filters).offset(offset).limit(page_size)
    logs = db.scalars(logs_query).all()

    start_page = max(page - 2, 1)
    end_page = min(start_page + 4, total_pages)
    start_page = max(end_page - 4, 1)

    return {
        "logs": logs,
        "page": page,
        "page_size": page_size,
        "total_count": total_count,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "page_numbers": list(range(start_page, end_page + 1)),
    }


def get_order(db: Session, order_id: int) -> Order | None:
    return db.scalar(order_query(db).where(Order.id == order_id))


def dashboard_stats(db: Session) -> dict:
    orders = db.scalars(order_query(db)).unique().all()
    recent_orders = orders[:8]
    return {
        "order_total": len(orders),
        "shipment_done_total": sum(1 for item in orders if item.shipment_status == "已发货"),
        "payment_done_total": sum(1 for item in orders if item.payment_status == "已付款"),
        "invoice_done_total": sum(1 for item in orders if item.invoice_status == "已开票"),
        "active_follow_up_total": sum(
            1
            for item in orders
            if item.order_status != "已完成"
            or item.shipment_status != "已发货"
            or item.payment_status != "已付款"
            or item.invoice_status != "已开票"
        ),
        "recent_orders": recent_orders,
        "order_amount_total": sum((item.order_amount for item in orders), Decimal("0.00")),
        "paid_amount_total": sum((item.paid_amount for item in orders), Decimal("0.00")),
        "unpaid_amount_total": sum((item.order_amount - item.paid_amount for item in orders), Decimal("0.00")),
    }


def build_order_payload(form) -> dict:
    return {
        "order_no": (form.get("order_no") or "").strip(),
        "customer_name": (form.get("customer_name") or "").strip(),
        "project_name": (form.get("project_name") or "").strip(),
        "order_date": parse_date(form.get("order_date")) or date.today(),
        "order_status": (form.get("order_status") or "执行中").strip(),
        "remark": (form.get("remark") or "").strip() or None,
    }


def save_order_contract(order: Order, upload: UploadFile | None) -> None:
    if not upload or not upload.filename or not order.id:
        return
    target = order_contract_upload_path(order.id, upload.filename)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as handle:
        handle.write(upload.file.read())
    order.contract_file_path = str(target.relative_to(ORDER_CONTRACT_UPLOAD_DIR))
    order.contract_original_filename = upload.filename


def create_order(db: Session, form, upload: UploadFile | None = None, operator_name: str | None = None) -> Order:
    order = Order(**build_order_payload(form))
    order.operator_name = operator_name or None
    recalculate_order(order)
    db.add(order)
    db.commit()
    db.refresh(order)
    if upload and upload.filename:
        save_order_contract(order, upload)
        db.commit()
        db.refresh(order)
    return order


def update_order(db: Session, order: Order, form, upload: UploadFile | None = None, operator_name: str | None = None) -> Order:
    for field, value in build_order_payload(form).items():
        setattr(order, field, value)
    if operator_name:
        order.operator_name = operator_name
    if upload and upload.filename:
        save_order_contract(order, upload)
    recalculate_order(order)
    db.commit()
    db.refresh(order)
    return order


def void_order(db: Session, order: Order) -> None:
    order.is_deleted = True
    order.order_status = "已作废"
    db.commit()


def restore_order(db: Session, order: Order) -> None:
    order.is_deleted = False
    if order.order_status == "已作废":
        order.order_status = "执行中"
    db.commit()


def remove_order_files(order: Order) -> None:
    if order.contract_file_path:
        contract_path = ORDER_CONTRACT_UPLOAD_DIR / order.contract_file_path
        if contract_path.exists():
            contract_path.unlink()

    for product in order.products:
        if not product.file_path:
            continue
        for base_dir in (PRODUCT_UPLOAD_DIR, LEGACY_CONTRACT_UPLOAD_DIR):
            product_path = base_dir / product.file_path
            if product_path.exists():
                product_path.unlink()
                break


def permanently_delete_order(db: Session, order: Order) -> None:
    remove_order_files(order)
    db.delete(order)
    db.commit()


def create_or_update_product(db: Session, order: Order, form, upload: UploadFile | None) -> None:
    product_id = form.get("product_id")
    product = db.get(Product, int(product_id)) if product_id else Product(order=order)
    if product.id is None:
        db.add(product)
    catalog_id = form.get("catalog_id")
    catalog = db.get(ProductCatalog, int(catalog_id)) if catalog_id and str(catalog_id).isdigit() else None
    if catalog is None:
        raise ValueError("invalid catalog")
    unit_price = decimal_or_none(form.get("unit_price"))
    if unit_price is None:
        unit_price = catalog.unit_price
    quantity = integer_decimal_or_none(form.get("quantity"))
    product.catalog = catalog
    product.product_no = catalog.product_no
    product.product_name = catalog.product_name
    product.unit_price = unit_price
    product.quantity = quantity
    product.product_amount = calculate_product_amount(unit_price, quantity, decimal_or_none(form.get("product_amount")))
    product.remark = (form.get("remark") or "").strip() or None
    recalculate_order(order)
    db.commit()


def create_or_update_shipment(db: Session, order: Order, form) -> None:
    shipment_id = form.get("shipment_id")
    shipment = db.get(Shipment, int(shipment_id)) if shipment_id else Shipment(order=order)
    if shipment.id is None:
        db.add(shipment)
    shipment.shipment_date = parse_date(form.get("shipment_date"))
    shipment.shipment_batch = (form.get("shipment_batch") or "").strip() or None
    shipment.shipment_amount = Decimal("0.00")
    shipment.shipment_status = (form.get("shipment_status") or "未发货").strip()
    shipment.shipment_content = (form.get("shipment_content") or "").strip() or None
    shipment.receiver_info = (form.get("receiver_info") or "").strip() or None
    shipment.receiver_name = None
    shipment.receiver_phone = None
    shipment.receiver_address = None
    shipment.logistics_company = (form.get("logistics_company") or "").strip() or None
    shipment.tracking_no = (form.get("tracking_no") or "").strip() or None
    shipment.remark = (form.get("remark") or "").strip() or None
    recalculate_order(order)
    db.commit()


def create_or_update_payment(db: Session, order: Order, form) -> None:
    payment_id = form.get("payment_id")
    payment = db.get(Payment, int(payment_id)) if payment_id else Payment(order=order)
    if payment.id is None:
        db.add(payment)
    payment_amount = parse_decimal(form.get("payment_amount"))
    validate_amount_not_exceed_order(order, payment_amount, "付款金额")
    payment.payment_date = parse_date(form.get("payment_date"))
    payment.payment_stage = (form.get("payment_stage") or "").strip() or None
    payment.payment_amount = payment_amount
    payment.payment_method = (form.get("payment_method") or "").strip() or None
    payment.remark = (form.get("remark") or "").strip() or None
    recalculate_order(order)
    db.commit()


def create_or_update_invoice(db: Session, order: Order, form) -> None:
    invoice_id = form.get("invoice_id")
    invoice = db.get(Invoice, int(invoice_id)) if invoice_id else Invoice(order=order)
    if invoice.id is None:
        db.add(invoice)
    invoice_amount = parse_decimal(form.get("invoice_amount"))
    validate_amount_not_exceed_order(order, invoice_amount, "开票金额")
    invoice.invoice_no = (form.get("invoice_no") or "").strip() or None
    invoice.invoice_date = parse_date(form.get("invoice_date"))
    invoice.invoice_amount = invoice_amount
    invoice.invoice_type = (form.get("invoice_type") or "").strip() or None
    invoice.remark = (form.get("remark") or "").strip() or None
    recalculate_order(order)
    db.commit()


def delete_record(db: Session, order: Order, model, item_id: int) -> None:
    item = db.get(model, item_id)
    if item is None:
        return
    db.delete(item)
    db.flush()
    db.expire(order, ["products", "shipments", "payments", "invoices"])
    recalculate_order(order)
    db.commit()


def add_operation_log(db: Session, order: Order, operator_name: str | None, action_type: str, content: str) -> None:
    db.add(
        OperationLog(
            order=order,
            operator_name=operator_name or None,
            action_type=action_type,
            content=content,
        )
    )
    db.commit()


def add_system_log(
    db: Session,
    operator_name: str | None,
    module_name: str,
    content: str,
    order_no: str | None = None,
) -> None:
    db.add(
        SystemLog(
            operator_name=operator_name or None,
            module_name=module_name,
            order_no=order_no or None,
            content=content,
        )
    )
    db.commit()


def list_users(db: Session) -> list[User]:
    return db.scalars(select(User).order_by(User.created_at.desc())).all()


def list_product_catalogs(db: Session) -> list[ProductCatalog]:
    return db.scalars(select(ProductCatalog).order_by(ProductCatalog.created_at.desc())).all()


def get_product_catalog(db: Session, catalog_id: int) -> ProductCatalog | None:
    return db.get(ProductCatalog, catalog_id)


def create_product_catalog(db: Session, form) -> None:
    db.add(
        ProductCatalog(
            product_no=(form.get("product_no") or "").strip() or None,
            product_name=(form.get("product_name") or "").strip() or "未命名产品",
            unit_price=decimal_or_none(form.get("unit_price")),
            remark=(form.get("remark") or "").strip() or None,
        )
    )
    db.commit()


def update_product_catalog(db: Session, catalog: ProductCatalog, form) -> None:
    catalog.product_no = (form.get("product_no") or "").strip() or None
    catalog.product_name = (form.get("product_name") or "").strip() or catalog.product_name
    catalog.unit_price = decimal_or_none(form.get("unit_price"))
    catalog.remark = (form.get("remark") or "").strip() or None
    for product in catalog.products:
        product.product_no = catalog.product_no
        product.product_name = catalog.product_name
        product.unit_price = catalog.unit_price
        product.product_amount = calculate_product_amount(product.unit_price, product.quantity, None)
    db.commit()


def delete_product_catalog(db: Session, catalog: ProductCatalog) -> bool:
    if catalog.products:
        return False
    db.delete(catalog)
    db.commit()
    return True


def create_user(db: Session, form) -> None:
    password = (form.get("password") or "").strip() or "123456"
    db.add(
        User(
            username=(form.get("username") or "").strip(),
            real_name=(form.get("real_name") or "").strip(),
            role=(form.get("role") or "user").strip(),
            is_active=form.get("is_active") == "on",
            password_hash=generate_password_hash(password),
        )
    )
    db.commit()


def update_user(db: Session, user: User, form) -> None:
    user.real_name = (form.get("real_name") or "").strip() or user.real_name
    user.role = (form.get("role") or "user").strip()
    user.is_active = form.get("is_active") == "on"
    password = (form.get("password") or "").strip()
    if password:
        user.password_hash = generate_password_hash(password)
    db.commit()


def delete_user(db: Session, user: User) -> None:
    db.delete(user)
    db.commit()


def admin_user_count(db: Session) -> int:
    return len(db.scalars(select(User).where(User.role == "admin")).all())


def user_can_manage(current_user: User | None) -> bool:
    return bool(current_user and current_user.role == "admin")
