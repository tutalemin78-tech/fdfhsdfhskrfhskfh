import time,json,hashlib,hmac
from urllib.parse import urlencode
from datetime import datetime,timezone
import pytest
import security,product,scheduling,config
from cryptography.fernet import Fernet


def signed(user=11111,age=0,extra=None):
    values={'user':json.dumps({'id':user,'first_name':'Test'}),'auth_date':str(int(time.time())-age)}
    values.update(extra or {})
    key=hmac.new(b'WebAppData',config.BOT_TOKEN.encode(),hashlib.sha256).digest()
    values['hash']=hmac.new(key,'\n'.join(f'{k}={values[k]}' for k in sorted(values)).encode(),hashlib.sha256).hexdigest()
    return urlencode(values)


def test_initdata_valid(): assert security.verify_init_data(signed(),config.BOT_TOKEN)['id']==11111
@pytest.mark.parametrize('age',[901,86400,1000000000,-60])
def test_expired_future_initdata(age): assert security.verify_init_data(signed(age=age),config.BOT_TOKEN) is None

def test_tampered_initdata(): assert security.verify_init_data(signed().replace('11111','22222'),config.BOT_TOKEN) is None

def test_duplicate_initdata(): assert security.verify_init_data(signed()+'&auth_date=1',config.BOT_TOKEN) is None

def test_token_signature_expiry():
    token=security.issue_token({'id':11111},config.BOT_TOKEN)
    assert security.read_token(token,config.BOT_TOKEN)=={'id':11111}
    assert security.read_token(token,'different') is None
    assert security.read_token(security.issue_token({'id':11111},config.BOT_TOKEN,ttl=-1),config.BOT_TOKEN) is None

def test_encryption_and_rotation(monkeypatch):
    original=security.encrypt('sensitive-test-data')
    assert 'sensitive' not in original and security.decrypt(original)=='sensitive-test-data'
    old=__import__('os').environ['SESSION_ENCRYPTION_KEYS']
    monkeypatch.setenv('SESSION_ENCRYPTION_KEYS',Fernet.generate_key().decode()+','+old)
    assert security.decrypt(original)=='sensitive-test-data'
    assert security.decrypt(security.encrypt(original))=='sensitive-test-data'
    with pytest.raises(RuntimeError): security.decrypt('plaintext')

def test_no_key_fail_closed(monkeypatch):
    monkeypatch.delenv('SESSION_ENCRYPTION_KEYS')
    with pytest.raises(RuntimeError): security.encrypt('test')

@pytest.mark.parametrize('text',['Hello','',product.FOOTER,'Hello\n\n'+product.FOOTER])
def test_footer_once(text):
    result=product.branded(text)
    assert result.count(product.FOOTER)==1
    assert result==product.branded(result)
    if text.startswith('Hello'): assert result=='Hello\n\n'+product.FOOTER

def test_utf16_limit():
    assert product.visible_units('😀')==2
    with pytest.raises(ValueError): product.validate_payload({'type':'text','text':'x'*4096})

def test_dst_nonexistent_ambiguous():
    with pytest.raises(ValueError): scheduling.parse_local('2027-03-28T02:30','Europe/Berlin')
    with pytest.raises(ValueError): scheduling.parse_local('2026-10-25T02:30','Europe/Berlin')

def test_daily_dst_wallclock():
    previous=scheduling.parse_local('2027-03-27T18:30','Europe/Berlin')
    nxt=scheduling.next_occurrence(previous,'Europe/Berlin','daily',[],now=previous)
    assert (nxt-previous).total_seconds()==23*3600
    assert nxt.astimezone(__import__('zoneinfo').ZoneInfo('Europe/Berlin')).hour==18

def test_weekday_schedule():
    start=scheduling.parse_local('2026-10-19T12:00','Europe/Berlin') # Monday
    nxt=scheduling.next_occurrence(start,'Europe/Berlin','weekly',[0,2],now=start)
    assert nxt.weekday()==2


def test_button_label_included_in_size_check():
    with pytest.raises(ValueError):product.validate_payload({'type':'text','text':'x'*4000,'buttons':[{'text':'y'*100,'url':'https://example.com'}]})


def test_dangerous_button_scheme_rejected():
    with pytest.raises(ValueError):product.with_links('Hello',[{'url':'javascript:alert(1)','text':'Click'}])
