from datetime import datetime, timedelta
from models import Order


def check_after_sales_eligibility(user_id, order_id):
    order = Order.query.get(order_id)
    if not order or order.user_id != user_id:
        return {"ok": False, "reason": "未找到订单或无权限"}

    if order.status in (5, 7):
        return {"ok": False, "reason": "该订单已取消或已完成售后"}

    if order.order_date and datetime.now() - order.order_date > timedelta(days=7):
        return {"ok": False, "reason": "已超过7天售后申请时限"}

    return {"ok": True, "reason": "满足基础售后条件"}
