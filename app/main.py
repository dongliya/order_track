from contextlib import asynccontextmanager
from decimal import Decimal
import mimetypes
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app.db import SessionLocal, get_db
from app.models import Invoice, Order, Payment, Product, Shipment, User
from app.services import (
    add_operation_log,
    add_system_log,
    authenticate,
    create_or_update_product,
    create_or_update_invoice,
    create_or_update_payment,
    create_or_update_shipment,
    create_order,
    create_user,
    dashboard_stats,
    delete_user,
    delete_record,
    ensure_seed_data,
    get_current_user,
    get_order,
    get_product_catalog,
    init_db,
    list_product_catalogs,
    list_orders,
    paginate_deleted_orders,
    paginate_system_logs,
    list_users,
    logout,
    admin_user_count,
    paginate_orders,
    permanently_delete_order,
    pop_flash,
    restore_order,
    save_login,
    set_flash,
    create_product_catalog,
    delete_product_catalog,
    update_product_catalog,
    update_order,
    update_user,
    user_can_manage,
    void_order,
)

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    db = SessionLocal()
    try:
        ensure_seed_data(db)
    finally:
        db.close()
    yield


app = FastAPI(title="OrderFlow", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key="order-track-dev-secret", same_site="lax")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(BASE_DIR / "static" / "favicon.ico")


def money(value: Decimal | None) -> str:
    if value is None:
        return "-"
    return f"{Decimal(value):,.2f}"


templates.env.filters["money"] = money


def page_context(request: Request, current_user: User | None, **extra):
    return {
        "request": request,
        "current_user": current_user,
        "flash": pop_flash(request),
        **extra,
    }


def money_text(value: Decimal | None) -> str:
    return money(value if value is not None else Decimal("0.00"))


class AuthRequired(Exception):
    pass


def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    current_user = get_current_user(request, db)
    if current_user is None:
        raise AuthRequired
    return current_user


@app.exception_handler(AuthRequired)
async def auth_required_handler(request: Request, exc: AuthRequired):
    return RedirectResponse(url="/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    if current_user:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", page_context(request, None))


@app.post("/login")
async def login(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    user = authenticate(db, (form.get("username") or "").strip(), form.get("password") or "")
    if user is None:
        set_flash(request, "用户名或密码错误", "error")
        return RedirectResponse("/login", status_code=303)
    save_login(request, user)
    set_flash(request, f"欢迎回来，{user.real_name}", "success")
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def do_logout(request: Request):
    logout(request)
    set_flash(request, "你已退出系统", "info")
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db), current_user: User = Depends(require_user)):
    stats = dashboard_stats(db)
    return templates.TemplateResponse("dashboard.html", page_context(request, current_user, stats=stats))


@app.get("/orders", response_class=HTMLResponse)
def order_list(request: Request, db: Session = Depends(get_db), current_user: User = Depends(require_user)):
    filters = {key: request.query_params.get(key, "").strip() for key in [
        "order_no",
        "customer_name",
        "project_name",
        "date_from",
        "date_to",
        "product_status",
        "shipment_status",
        "payment_status",
        "invoice_status",
        "order_status",
    ]}
    raw_page = request.query_params.get("page", "1").strip()
    page = int(raw_page) if raw_page.isdigit() else 1
    pagination = paginate_orders(db, filters, page=page, page_size=10)
    return templates.TemplateResponse(
        "orders.html",
        page_context(
            request,
            current_user,
            orders=pagination["orders"],
            filters=filters,
            pagination=pagination,
        ),
    )


@app.get("/orders/deleted", response_class=HTMLResponse)
def deleted_order_list(request: Request, db: Session = Depends(get_db), current_user: User = Depends(require_user)):
    if not user_can_manage(current_user):
        set_flash(request, "只有管理员可以查看已作废订单", "error")
        return RedirectResponse("/orders", status_code=303)
    filters = {key: request.query_params.get(key, "").strip() for key in [
        "order_no",
        "customer_name",
        "project_name",
        "date_from",
        "date_to",
        "product_status",
        "shipment_status",
        "payment_status",
        "invoice_status",
        "order_status",
    ]}
    raw_page = request.query_params.get("page", "1").strip()
    page = int(raw_page) if raw_page.isdigit() else 1
    pagination = paginate_deleted_orders(db, filters, page=page, page_size=10)
    return templates.TemplateResponse(
        "deleted_orders.html",
        page_context(
            request,
            current_user,
            orders=pagination["orders"],
            filters=filters,
            pagination=pagination,
        ),
    )


@app.get("/orders/new", response_class=HTMLResponse)
def order_new_page(request: Request, db: Session = Depends(get_db), current_user: User = Depends(require_user)):
    return templates.TemplateResponse(
        "order_form.html",
        page_context(request, current_user, order=None, page_title="新建订单"),
    )


@app.post("/orders/new")
async def order_create(
    request: Request,
    contract_file: UploadFile | None = File(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user),
):
    form = await request.form()
    try:
        order = create_order(db, form, upload=contract_file, operator_name=current_user.real_name)
    except IntegrityError:
        db.rollback()
        set_flash(request, "订单编号已存在，请更换后重试", "error")
        return RedirectResponse("/orders/new", status_code=303)
    add_system_log(
        db,
        current_user.real_name,
        "订单管理",
        f"新建订单：订单编号 {order.order_no}，客户 {order.customer_name}，项目 {order.project_name}，订单日期 {order.order_date}，订单状态 {order.order_status}",
        order_no=order.order_no,
    )
    if contract_file and contract_file.filename:
        add_operation_log(db, order, current_user.real_name, "合同附件", f"上传合同附件：{contract_file.filename}")
    set_flash(request, "订单已创建", "success")
    return RedirectResponse(f"/orders/{order.id}", status_code=303)


@app.get("/orders/{order_id}/edit", response_class=HTMLResponse)
def order_edit_page(order_id: int, request: Request, db: Session = Depends(get_db), current_user: User = Depends(require_user)):
    order = get_order(db, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return templates.TemplateResponse(
        "order_form.html",
        page_context(request, current_user, order=order, page_title="编辑订单"),
    )


@app.post("/orders/{order_id}/edit")
async def order_edit(
    order_id: int,
    request: Request,
    contract_file: UploadFile | None = File(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user),
):
    order = get_order(db, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    form = await request.form()
    try:
        update_order(db, order, form, upload=contract_file, operator_name=current_user.real_name)
    except IntegrityError:
        db.rollback()
        set_flash(request, "订单编号已存在，请更换后重试", "error")
        return RedirectResponse(f"/orders/{order_id}/edit", status_code=303)
    add_operation_log(
        db,
        order,
        current_user.real_name,
        "订单",
        f"更新订单信息：订单编号 {order.order_no}，客户 {order.customer_name}，项目 {order.project_name}，订单日期 {order.order_date}，订单状态 {order.order_status}",
    )
    if contract_file and contract_file.filename:
        add_operation_log(db, order, current_user.real_name, "合同附件", f"上传合同附件：{contract_file.filename}")
    set_flash(request, "订单已更新", "success")
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


@app.post("/orders/{order_id}/delete")
def order_delete(order_id: int, request: Request, db: Session = Depends(get_db), current_user: User = Depends(require_user)):
    order = get_order(db, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    add_system_log(
        db,
        current_user.real_name,
        "订单管理",
        f"作废订单：订单编号 {order.order_no}，客户 {order.customer_name}，项目 {order.project_name}",
        order_no=order.order_no,
    )
    void_order(db, order)
    set_flash(request, "订单已作废", "info")
    return RedirectResponse("/orders", status_code=303)


@app.post("/orders/{order_id}/restore")
def order_restore(order_id: int, request: Request, db: Session = Depends(get_db), current_user: User = Depends(require_user)):
    if not user_can_manage(current_user):
        set_flash(request, "只有管理员可以恢复订单", "error")
        return RedirectResponse("/orders", status_code=303)
    order = db.get(Order, order_id)
    if order is None or not order.is_deleted:
        raise HTTPException(status_code=404, detail="Deleted order not found")
    restore_order(db, order)
    add_system_log(
        db,
        current_user.real_name,
        "订单管理",
        f"恢复订单：订单编号 {order.order_no}，客户 {order.customer_name}，项目 {order.project_name}",
        order_no=order.order_no,
    )
    set_flash(request, "订单已恢复", "success")
    return RedirectResponse("/orders/deleted", status_code=303)


@app.post("/orders/{order_id}/purge")
def order_purge(order_id: int, request: Request, db: Session = Depends(get_db), current_user: User = Depends(require_user)):
    if not user_can_manage(current_user):
        set_flash(request, "只有管理员可以彻底删除订单", "error")
        return RedirectResponse("/orders", status_code=303)
    order = db.get(Order, order_id)
    if order is None or not order.is_deleted:
        raise HTTPException(status_code=404, detail="Deleted order not found")
    order_no = order.order_no
    customer_name = order.customer_name
    project_name = order.project_name
    permanently_delete_order(db, order)
    add_system_log(
        db,
        current_user.real_name,
        "订单管理",
        f"彻底删除订单：订单编号 {order_no}，客户 {customer_name}，项目 {project_name}",
        order_no=order_no,
    )
    set_flash(request, "订单已彻底删除", "success")
    return RedirectResponse("/orders/deleted", status_code=303)


@app.get("/orders/{order_id}", response_class=HTMLResponse)
def order_detail(order_id: int, request: Request, db: Session = Depends(get_db), current_user: User = Depends(require_user)):
    order = get_order(db, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    tab = request.query_params.get("tab", "overview")
    show_new = {
        "product": request.query_params.get("new_product") == "1",
        "shipment": request.query_params.get("new_shipment") == "1",
        "payment": request.query_params.get("new_payment") == "1",
        "invoice": request.query_params.get("new_invoice") == "1",
    }
    edit_ids = {
        "product": request.query_params.get("edit_product_id"),
        "shipment": request.query_params.get("edit_shipment_id"),
        "payment": request.query_params.get("edit_payment_id"),
        "invoice": request.query_params.get("edit_invoice_id"),
    }
    editing = {
        "product": next((item for item in order.products if str(item.id) == edit_ids["product"]), None),
        "shipment": next((item for item in order.shipments if str(item.id) == edit_ids["shipment"]), None),
        "payment": next((item for item in order.payments if str(item.id) == edit_ids["payment"]), None),
        "invoice": next((item for item in order.invoices if str(item.id) == edit_ids["invoice"]), None),
    }
    return templates.TemplateResponse(
        "order_detail.html",
        page_context(
            request,
            current_user,
            order=order,
            tab=tab,
            editing=editing,
            show_new=show_new,
            product_catalogs=list_product_catalogs(db),
        ),
    )


@app.post("/orders/{order_id}/products")
async def product_upsert(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user),
):
    order = get_order(db, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    form = await request.form()
    product_id = form.get("product_id")
    try:
        create_or_update_product(db, order, form, None)
    except ValueError:
        db.rollback()
        set_flash(request, "请选择产品资料库中的产品", "error")
        return RedirectResponse(f"/orders/{order_id}?tab=products", status_code=303)
    selected_product = (
        next((item for item in order.products if str(item.id) == product_id), None)
        if product_id
        else max(order.products, key=lambda item: item.id, default=None)
    )
    action_name = "编辑" if product_id else "新增"
    if selected_product:
        add_operation_log(
            db,
            order,
            current_user.real_name,
            "产品",
            f"{action_name}产品：{selected_product.product_name}，编号 {selected_product.product_no or '-'}，单价 {money_text(selected_product.unit_price)}，数量 {selected_product.quantity or 0}，金额 {money_text(selected_product.product_amount)}",
        )
    set_flash(request, "产品信息已保存", "success")
    return RedirectResponse(f"/orders/{order_id}?tab=products", status_code=303)


@app.post("/orders/{order_id}/shipments")
async def shipment_upsert(order_id: int, request: Request, db: Session = Depends(get_db), current_user: User = Depends(require_user)):
    order = get_order(db, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    form = await request.form()
    shipment_id = form.get("shipment_id")
    create_or_update_shipment(db, order, form)
    shipment = (
        next((item for item in order.shipments if str(item.id) == shipment_id), None)
        if shipment_id
        else max(order.shipments, key=lambda item: item.id, default=None)
    )
    if shipment:
        add_operation_log(
            db,
            order,
            current_user.real_name,
            "发货",
            (
                f"{'编辑' if shipment_id else '新增'}发货记录："
                f"日期 {shipment.shipment_date or '-'}，批次 {shipment.shipment_batch or '-'}，状态 {shipment.shipment_status or '-'}，"
                f"物流公司 {shipment.logistics_company or '-'}，物流单号 {shipment.tracking_no or '-'}"
            ),
        )
    set_flash(request, "发货记录已保存", "success")
    return RedirectResponse(f"/orders/{order_id}?tab=shipments", status_code=303)


@app.post("/orders/{order_id}/payments")
async def payment_upsert(order_id: int, request: Request, db: Session = Depends(get_db), current_user: User = Depends(require_user)):
    order = get_order(db, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    form = await request.form()
    payment_id = form.get("payment_id")
    try:
        create_or_update_payment(db, order, form)
    except ValueError as exc:
        db.rollback()
        set_flash(request, str(exc), "error")
        return RedirectResponse(f"/orders/{order_id}?tab=payments", status_code=303)
    payment = (
        next((item for item in order.payments if str(item.id) == payment_id), None)
        if payment_id
        else max(order.payments, key=lambda item: item.id, default=None)
    )
    if payment:
        add_operation_log(
            db,
            order,
            current_user.real_name,
            "付款",
            (
                f"{'编辑' if payment_id else '新增'}付款记录："
                f"日期 {payment.payment_date or '-'}，阶段 {payment.payment_stage or '-'}，金额 {money_text(payment.payment_amount)}，付款方式 {payment.payment_method or '-'}"
            ),
        )
    set_flash(request, "付款记录已保存", "success")
    return RedirectResponse(f"/orders/{order_id}?tab=payments", status_code=303)


@app.post("/orders/{order_id}/invoices")
async def invoice_upsert(order_id: int, request: Request, db: Session = Depends(get_db), current_user: User = Depends(require_user)):
    order = get_order(db, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    form = await request.form()
    invoice_id = form.get("invoice_id")
    try:
        create_or_update_invoice(db, order, form)
    except ValueError as exc:
        db.rollback()
        set_flash(request, str(exc), "error")
        return RedirectResponse(f"/orders/{order_id}?tab=invoices", status_code=303)
    invoice = (
        next((item for item in order.invoices if str(item.id) == invoice_id), None)
        if invoice_id
        else max(order.invoices, key=lambda item: item.id, default=None)
    )
    if invoice:
        add_operation_log(
            db,
            order,
            current_user.real_name,
            "发票",
            (
                f"{'编辑' if invoice_id else '新增'}发票记录："
                f"发票号码 {invoice.invoice_no or '-'}，开票日期 {invoice.invoice_date or '-'}，金额 {money_text(invoice.invoice_amount)}，类型 {invoice.invoice_type or '-'}"
            ),
        )
    set_flash(request, "发票记录已保存", "success")
    return RedirectResponse(f"/orders/{order_id}?tab=invoices", status_code=303)


@app.post("/orders/{order_id}/products/{product_id}/delete")
def product_delete(order_id: int, product_id: int, request: Request, db: Session = Depends(get_db), current_user: User = Depends(require_user)):
    order = get_order(db, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    product = db.get(Product, product_id)
    delete_record(db, order, Product, product_id)
    add_operation_log(
        db,
        order,
        current_user.real_name,
        "产品",
        (
            f"删除产品：{product.product_name if product else product_id}，"
            f"编号 {product.product_no if product and product.product_no else '-'}，"
            f"数量 {product.quantity if product and product.quantity is not None else 0}，"
            f"金额 {money_text(product.product_amount if product else Decimal('0.00'))}"
        ),
    )
    set_flash(request, "产品记录已删除", "info")
    return RedirectResponse(f"/orders/{order_id}?tab=products", status_code=303)


@app.post("/orders/{order_id}/shipments/{shipment_id}/delete")
def shipment_delete(order_id: int, shipment_id: int, request: Request, db: Session = Depends(get_db), current_user: User = Depends(require_user)):
    order = get_order(db, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    shipment = db.get(Shipment, shipment_id)
    delete_record(db, order, Shipment, shipment_id)
    add_operation_log(
        db,
        order,
        current_user.real_name,
        "发货",
        (
            f"删除发货记录：日期 {shipment.shipment_date if shipment and shipment.shipment_date else '-'}，"
            f"批次 {shipment.shipment_batch if shipment and shipment.shipment_batch else '-'}，"
            f"状态 {shipment.shipment_status if shipment and shipment.shipment_status else '-'}"
        ),
    )
    set_flash(request, "发货记录已删除", "info")
    return RedirectResponse(f"/orders/{order_id}?tab=shipments", status_code=303)


@app.post("/orders/{order_id}/payments/{payment_id}/delete")
def payment_delete(order_id: int, payment_id: int, request: Request, db: Session = Depends(get_db), current_user: User = Depends(require_user)):
    order = get_order(db, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    payment = db.get(Payment, payment_id)
    delete_record(db, order, Payment, payment_id)
    add_operation_log(
        db,
        order,
        current_user.real_name,
        "付款",
        (
            f"删除付款记录：日期 {payment.payment_date if payment and payment.payment_date else '-'}，"
            f"阶段 {payment.payment_stage if payment and payment.payment_stage else '-'}，"
            f"金额 {money_text(payment.payment_amount if payment else Decimal('0.00'))}"
        ),
    )
    set_flash(request, "付款记录已删除", "info")
    return RedirectResponse(f"/orders/{order_id}?tab=payments", status_code=303)


@app.post("/orders/{order_id}/invoices/{invoice_id}/delete")
def invoice_delete(order_id: int, invoice_id: int, request: Request, db: Session = Depends(get_db), current_user: User = Depends(require_user)):
    order = get_order(db, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    invoice = db.get(Invoice, invoice_id)
    delete_record(db, order, Invoice, invoice_id)
    add_operation_log(
        db,
        order,
        current_user.real_name,
        "发票",
        (
            f"删除发票记录：发票号码 {invoice.invoice_no if invoice and invoice.invoice_no else '-'}，"
            f"开票日期 {invoice.invoice_date if invoice and invoice.invoice_date else '-'}，"
            f"金额 {money_text(invoice.invoice_amount if invoice else Decimal('0.00'))}"
        ),
    )
    set_flash(request, "发票记录已删除", "info")
    return RedirectResponse(f"/orders/{order_id}?tab=invoices", status_code=303)


@app.get("/products/{product_id}/download")
def product_download(product_id: int, request: Request, db: Session = Depends(get_db), current_user: User = Depends(require_user)):
    product = db.get(Product, product_id)
    if product is None or not product.file_path:
        raise HTTPException(status_code=404, detail="Product file not found")
    path = BASE_DIR / "uploads" / "products" / product.file_path
    if not path.exists():
        path = BASE_DIR / "uploads" / "contracts" / product.file_path
    if not path.exists():
        raise HTTPException(status_code=404, detail="Product file missing")
    return FileResponse(path, filename=product.original_filename or path.name)


@app.get("/orders/{order_id}/contract/download")
def order_contract_download(order_id: int, request: Request, db: Session = Depends(get_db), current_user: User = Depends(require_user)):
    order = get_order(db, order_id)
    if order is None or not order.contract_file_path:
        raise HTTPException(status_code=404, detail="Contract file not found")
    path = BASE_DIR / "uploads" / "order_contracts" / order.contract_file_path
    if not path.exists():
        raise HTTPException(status_code=404, detail="Contract file missing")
    return FileResponse(path, filename=order.contract_original_filename or path.name)


@app.get("/orders/{order_id}/contract/view")
def order_contract_view(order_id: int, request: Request, db: Session = Depends(get_db), current_user: User = Depends(require_user)):
    order = get_order(db, order_id)
    if order is None or not order.contract_file_path:
        raise HTTPException(status_code=404, detail="Contract file not found")
    path = BASE_DIR / "uploads" / "order_contracts" / order.contract_file_path
    if not path.exists():
        raise HTTPException(status_code=404, detail="Contract file missing")
    media_type, _ = mimetypes.guess_type(str(path))
    return FileResponse(
        path,
        media_type=media_type or "application/octet-stream",
        filename=order.contract_original_filename or path.name,
        content_disposition_type="inline",
    )


@app.post("/orders/{order_id}/contract/delete")
def order_contract_delete(order_id: int, request: Request, db: Session = Depends(get_db), current_user: User = Depends(require_user)):
    order = get_order(db, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.contract_file_path:
        path = BASE_DIR / "uploads" / "order_contracts" / order.contract_file_path
        if path.exists():
            path.unlink()

    original_filename = order.contract_original_filename
    order.contract_file_path = None
    order.contract_original_filename = None
    db.commit()
    add_operation_log(db, order, current_user.real_name, "合同附件", f"删除合同附件：{original_filename or '-'}")
    set_flash(request, "合同附件已删除", "info")
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


@app.get("/users/manage", response_class=HTMLResponse)
def user_page(request: Request, db: Session = Depends(get_db), current_user: User = Depends(require_user)):
    if not user_can_manage(current_user):
        set_flash(request, "只有管理员可以管理用户", "error")
        return RedirectResponse("/", status_code=303)
    edit_user_id = request.query_params.get("edit_user_id")
    editing_user = db.get(User, int(edit_user_id)) if edit_user_id else None
    return templates.TemplateResponse(
        "users.html",
        page_context(request, current_user, users=list_users(db), editing_user=editing_user),
    )


@app.get("/products/manage", response_class=HTMLResponse)
def product_manage_page(request: Request, db: Session = Depends(get_db), current_user: User = Depends(require_user)):
    if not user_can_manage(current_user):
        set_flash(request, "只有管理员可以管理产品", "error")
        return RedirectResponse("/", status_code=303)
    edit_catalog_id = request.query_params.get("edit_catalog_id")
    editing_catalog = get_product_catalog(db, int(edit_catalog_id)) if edit_catalog_id and edit_catalog_id.isdigit() else None
    return templates.TemplateResponse(
        "products_manage.html",
        page_context(request, current_user, catalogs=list_product_catalogs(db), editing_catalog=editing_catalog),
    )


@app.get("/system/logs", response_class=HTMLResponse)
def system_logs_page(request: Request, db: Session = Depends(get_db), current_user: User = Depends(require_user)):
    if not user_can_manage(current_user):
        set_flash(request, "只有管理员可以查看系统日志", "error")
        return RedirectResponse("/", status_code=303)
    filters = {key: request.query_params.get(key, "").strip() for key in [
        "order_no",
        "operator_name",
        "module_name",
        "keyword",
    ]}
    raw_page = request.query_params.get("page", "1").strip()
    page = int(raw_page) if raw_page.isdigit() else 1
    pagination = paginate_system_logs(db, filters, page=page, page_size=20)
    return templates.TemplateResponse(
        "system_logs.html",
        page_context(
            request,
            current_user,
            logs=pagination["logs"],
            filters=filters,
            pagination=pagination,
        ),
    )


@app.post("/products/manage")
async def product_manage_save(request: Request, db: Session = Depends(get_db), current_user: User = Depends(require_user)):
    if not user_can_manage(current_user):
        set_flash(request, "只有管理员可以管理产品", "error")
        return RedirectResponse("/", status_code=303)
    form = await request.form()
    catalog_id = form.get("catalog_id")
    try:
        if catalog_id:
            catalog = get_product_catalog(db, int(catalog_id))
            if catalog is None:
                raise HTTPException(status_code=404, detail="Product catalog not found")
            update_product_catalog(db, catalog, form)
            add_system_log(
                db,
                current_user.real_name,
                "产品管理",
                f"更新产品资料：产品编号 {catalog.product_no or '-'}，产品名称 {catalog.product_name}",
            )
            set_flash(request, "产品资料已更新", "success")
        else:
            create_product_catalog(db, form)
            add_system_log(
                db,
                current_user.real_name,
                "产品管理",
                f"新建产品资料：产品编号 {(form.get('product_no') or '').strip() or '-'}，产品名称 {(form.get('product_name') or '').strip() or '未命名产品'}",
            )
            set_flash(request, "产品资料已创建", "success")
    except IntegrityError:
        db.rollback()
        set_flash(request, "产品名称已存在，请更换后重试", "error")
    return RedirectResponse("/products/manage", status_code=303)


@app.post("/products/catalog/{catalog_id}/delete")
def product_manage_delete(catalog_id: int, request: Request, db: Session = Depends(get_db), current_user: User = Depends(require_user)):
    if not user_can_manage(current_user):
        set_flash(request, "只有管理员可以管理产品", "error")
        return RedirectResponse("/", status_code=303)
    catalog = get_product_catalog(db, catalog_id)
    if catalog is None:
        raise HTTPException(status_code=404, detail="Product catalog not found")
    if not delete_product_catalog(db, catalog):
        set_flash(request, "已有订单在使用该产品，暂不能删除", "error")
        return RedirectResponse("/products/manage", status_code=303)
    add_system_log(
        db,
        current_user.real_name,
        "产品管理",
        f"删除产品资料：产品编号 {catalog.product_no or '-'}，产品名称 {catalog.product_name}",
    )
    set_flash(request, "产品资料已删除", "success")
    return RedirectResponse("/products/manage", status_code=303)


@app.post("/users/manage")
async def user_save(request: Request, db: Session = Depends(get_db), current_user: User = Depends(require_user)):
    if not user_can_manage(current_user):
        set_flash(request, "只有管理员可以管理用户", "error")
        return RedirectResponse("/", status_code=303)
    form = await request.form()
    user_id = form.get("user_id")
    try:
        if user_id:
            user = db.get(User, int(user_id))
            if user is None:
                raise HTTPException(status_code=404, detail="User not found")
            update_user(db, user, form)
            add_system_log(
                db,
                current_user.real_name,
                "用户管理",
                f"更新用户：用户名 {user.username}，姓名 {user.real_name}，角色 {user.role}，状态 {'启用' if user.is_active else '停用'}",
            )
            set_flash(request, "用户信息已更新", "success")
        else:
            create_user(db, form)
            add_system_log(
                db,
                current_user.real_name,
                "用户管理",
                f"新建用户：用户名 {(form.get('username') or '').strip()}，姓名 {(form.get('real_name') or '').strip()}，角色 {(form.get('role') or 'user').strip()}",
            )
            set_flash(request, "用户已创建", "success")
    except IntegrityError:
        db.rollback()
        set_flash(request, "用户名已存在，请更换后重试", "error")
    return RedirectResponse("/users/manage", status_code=303)


@app.post("/users/{user_id}/delete")
def user_delete(user_id: int, request: Request, db: Session = Depends(get_db), current_user: User = Depends(require_user)):
    if not user_can_manage(current_user):
        set_flash(request, "只有管理员可以管理用户", "error")
        return RedirectResponse("/", status_code=303)

    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    if user.id == current_user.id:
        set_flash(request, "不能删除当前登录账号", "error")
        return RedirectResponse("/users/manage", status_code=303)

    if user.role == "admin" and admin_user_count(db) <= 1:
        set_flash(request, "至少需要保留一个管理员账号", "error")
        return RedirectResponse("/users/manage", status_code=303)

    delete_user(db, user)
    add_system_log(
        db,
        current_user.real_name,
        "用户管理",
        f"删除用户：用户名 {user.username}，姓名 {user.real_name}，角色 {user.role}",
    )
    set_flash(request, "用户已删除", "success")
    return RedirectResponse("/users/manage", status_code=303)
