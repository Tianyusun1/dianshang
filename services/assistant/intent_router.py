AFTER_SALES_KEYWORDS = ['退款', '退货', '换货', '售后', '物流', '发货', '订单']


def detect_intent(question):
    for kw in AFTER_SALES_KEYWORDS:
        if kw in question:
            return 'after_sales'
    return 'pre_sales'
