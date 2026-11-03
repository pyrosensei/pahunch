from app.language import recorded_turn_text


def test_server_order_facts_and_otp_reminder_remain_readable_in_opening():
    text = 'Order #1050 arrives 18:00-20:00. Give the OTP only to the courier.'
    for payload in [
        {'opening': True, 'planner': {'model': 'rules:delivery-opening'}},
        {'planner': {'model': 'rules:delivery-opening', 'fallback_reason': 'OUTBOUND_DELIVERY_PURPOSE'}},
    ]:
        assert recorded_turn_text(text, 'AGENT', payload) == text


def test_customer_and_model_code_disclosures_are_still_redacted():
    secret = 'My OTP is 123456'
    for speaker, payload in [
        ('CUSTOMER', {}), ('AGENT', {'planner': {'model': 'openai/gpt-oss-20b'}}),
        ('CUSTOMER', {'opening': True, 'planner': {'model': 'rules:delivery-opening'}}),
        ('AGENT', {'opening': True, 'planner': {'model': 'some-model'}}),
    ]:
        assert '123456' not in recorded_turn_text(secret, speaker, payload)
