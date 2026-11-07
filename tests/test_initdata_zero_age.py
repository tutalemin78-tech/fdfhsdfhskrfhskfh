import time,json,hashlib,hmac
from urllib.parse import urlencode
import pytest
import security,config


def signed(user=11111,age=0):
    values={'user':json.dumps({'id':user,'first_name':'Test'}),'auth_date':str(int(time.time())-age)}
    key=hmac.new(b'WebAppData',config.BOT_TOKEN.encode(),hashlib.sha256).digest()
    values['hash']=hmac.new(key,'\n'.join(f'{k}={values[k]}' for k in sorted(values)).encode(),hashlib.sha256).hexdigest()
    return urlencode(values)


def test_initdata_max_age_zero_disables_only_age_check():
    assert security.verify_init_data(signed(age=100000),config.BOT_TOKEN,max_age=0)['id']==11111
    assert security.verify_init_data(signed(age=100000),config.BOT_TOKEN,max_age=900) is None
    tampered=signed(age=100000).replace('11111','22222')
    assert security.verify_init_data(tampered,config.BOT_TOKEN,max_age=0) is None


def test_initdata_zero_age_still_requires_valid_auth_date():
    values={'user':json.dumps({'id':11111}),'auth_date':'not-a-date'}
    key=hmac.new(b'WebAppData',config.BOT_TOKEN.encode(),hashlib.sha256).digest()
    values['hash']=hmac.new(key,'\n'.join(f'{k}={values[k]}' for k in sorted(values)).encode(),hashlib.sha256).hexdigest()
    assert security.verify_init_data(urlencode(values),config.BOT_TOKEN,max_age=0) is None
