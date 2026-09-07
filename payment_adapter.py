"""Receipt adapters. Manual channels record verified money, never initiate payment."""
from flask import abort
class ManualReceiptAdapter:
    def validate(self,reference,environment):
        if not reference.strip():abort(400,description='请填写已核验的收据号或银行流水号')
        return reference
class MockPaymentAdapter:
    def validate(self,reference,environment):
        if environment not in {'dev','test'}:abort(403,description='生产环境禁止使用模拟支付')
        return 'MOCK-'+reference.removeprefix('MOCK-')
ADAPTERS={'cash':ManualReceiptAdapter(),'bank':ManualReceiptAdapter(),'mock':MockPaymentAdapter()}
def validate_receipt(channel,reference,environment):return ADAPTERS[channel].validate(reference,environment)
