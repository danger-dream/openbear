"""Signature test vectors taken from the providers' official documentation."""
import calendar
import time

from app.ddns.providers import _sign


def ts(text):
    return calendar.timegm(time.strptime(text, '%Y-%m-%dT%H:%M:%SZ'))


def test_aliyun_rpc_official_example():
    # https://help.aliyun.com/zh/sdk/product-overview/rpc-mechanism "固定参数示例"
    params = {'AccessKeyId': 'testid', 'Action': 'DescribeDedicatedHosts', 'Format': 'JSON', 'RegionId': 'cn-beijing',
              'SignatureMethod': 'HMAC-SHA1', 'SignatureNonce': 'edb2b34af0af9a6d14deaf7c1a5315eb', 'SignatureVersion': '1.0',
              'Timestamp': '2023-03-13T08:34:30Z', 'Version': '2014-05-26'}
    assert _sign.query_string(params) == (
        'AccessKeyId=testid&Action=DescribeDedicatedHosts&Format=JSON&RegionId=cn-beijing&SignatureMethod=HMAC-SHA1'
        '&SignatureNonce=edb2b34af0af9a6d14deaf7c1a5315eb&SignatureVersion=1.0&Timestamp=2023-03-13T08%3A34%3A30Z&Version=2014-05-26')
    assert _sign.aliyun_rpc('GET', params, 'testsecret') == '9NaGiOspFP5UPcwX8Iwt2YJXXuk='


def test_aliyun_encoding_rules():
    assert _sign.encode('a b*c~d/e') == 'a%20b%2Ac~d%2Fe'


def test_tencent_tc3_official_example():
    # https://cloud.tencent.com/document/api/1427/56189 "签名方法 v3"; the documented key is the literal masked string
    headers = _sign.tc3('AKID' + '*' * 32, '*' * 32, service='cvm', host='cvm.tencentcloudapi.com', action='DescribeInstances',
                        payload='{"Limit": 1, "Filters": [{"Values": ["\\u672a\\u547d\\u540d"], "Name": "instance-name"}]}',
                        timestamp=1551113065)
    assert headers['Authorization'] == (
        'TC3-HMAC-SHA256 Credential=AKID********************************/2019-02-25/cvm/tc3_request, '
        'SignedHeaders=content-type;host;x-tc-action, Signature=10b1a37a7301a02ca19a647ad722d5e43b4b3cff309d421d85b46093f6ab6c4f')
    assert headers['X-TC-Timestamp'] == '1551113065' and headers['Content-Type'] == 'application/json; charset=utf-8'


def test_huawei_official_example():
    # https://support.huaweicloud.com/devg-apisign/api-sign-algorithm-002.html; the documented SK is the literal masked string
    auth = _sign.huawei('QTWA***KYUC', 'MFyf***VmHc', method='GET', path='/v1/77b6a44cba5143ab91d13ab9a8ff44fd/vpcs',
                        query={'limit': '2', 'marker': '13551d6b-755d-4757-b956-536f674975c0'},
                        headers={'Content-Type': 'application/json', 'Host': 'service.region.example.com',
                                 'X-Sdk-Date': '20191115T033655Z'},
                        body=b'', stamp='20191115T033655Z')
    assert auth == ('SDK-HMAC-SHA256 Access=QTWA***KYUC, SignedHeaders=content-type;host;x-sdk-date, '
                    'Signature=f12f84a5ecf9eff3206499c4a55b13d1adad745dc8624a2e31f15c6b381d5b80')


def test_baidu_bce_official_example():
    # https://cloud.baidu.com/doc/Reference/s/njwvz1yfu "认证字符串示例"
    headers = {'Host': 'bj.bcebos.com', 'Content-Type': 'text/plain', 'Content-Length': '8',
               'Content-Md5': 'NFzcPqhviddjRNnSOGo4rw==', 'x-bce-date': '2015-04-27T08:23:49Z'}
    kw = dict(method='PUT', path='/v1/test/myfolder/readme.txt',
              query={'partNumber': '9', 'uploadId': 'a44cc9bab11cbd156984767aad637851'}, headers=headers,
              timestamp=ts('2015-04-27T08:23:49Z'))
    auth = _sign.bce('a' * 32, 'b' * 32, list_signed=False, **kw)
    assert auth == ('bce-auth-v1/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/2015-04-27T08:23:49Z/1800//'
                    'd74a04362e6a848f5b39b15421cb449427f419c95a480fd6b8cf9fc783e2999e')
    listed = _sign.bce('a' * 32, 'b' * 32, **kw)
    assert listed.endswith('/content-length;content-md5;content-type;host;x-bce-date/'
                           'd74a04362e6a848f5b39b15421cb449427f419c95a480fd6b8cf9fc783e2999e')


VOLC_AK = 'AKLTYWViMTVmZGYzM2E0NDI5Mzk2MDZjNjFmMjc2MjRjMzg'
VOLC_SK = 'WkRZeE1EQmxPVGhsWWpWak5HVmtNbUUxTXpZeU9UVXlOMlE1TmpZeVlqTQ=='


def test_volcengine_official_examples():
    # https://www.volcengine.com/docs/6369/67269 "签名方法", GET and POST examples
    common = dict(region='cn-beijing', service='billing', path='/', stamp='20250329T180937Z',
                  headers={'Host': 'billing.volcengineapi.com', 'X-Date': '20250329T180937Z'})
    get = _sign.volc(VOLC_AK, VOLC_SK, method='GET', query={'Action': 'QueryBalanceAcct', 'Version': '2022-01-01'}, body=b'', **common)
    assert get == (f'HMAC-SHA256 Credential={VOLC_AK}/20250329/cn-beijing/billing/request, SignedHeaders=host;x-date, '
                   'Signature=1eda9e7e6b1728151a8e8791fdaf67cfbd28bd5c80d0fce2eb208746cf483105')
    post = _sign.volc(VOLC_AK, VOLC_SK, method='POST', query={'Action': 'ListBill', 'Version': '2022-01-01'},
                      body=b'{"Limit":10,"BillPeriod":"2023-08"}', **common)
    assert post.endswith('Signature=5e8480ceea12d0000a23c054151c50dd02c1a7dec835004057d19f13d53a7658')


AWS = dict(region='us-east-1', service='service', stamp='20150830T123600Z')
AWS_KEYS = ('AKIDEXAMPLE', 'wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY')
AWS_HEADERS = {'Host': 'example.amazonaws.com', 'X-Amz-Date': '20150830T123600Z'}


def aws_auth(signature, signed='host;x-amz-date'):
    return f'AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, SignedHeaders={signed}, Signature={signature}'


def test_aws_sigv4_test_suite():
    # AWS Signature Version 4 test suite (aws-sig-v4-test-suite, mirrored in awslabs/aws-c-auth tests/aws-signing-test-suite/v4)
    cases = [
        ('GET', {}, b'', '5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31'),                        # get-vanilla
        ('GET', [('Param2', 'value2'), ('Param1', 'value1')], b'',
         'b97d918cfa904a5beff61c982a1b6f458b799221646efd99d3219ec94cdf2500'),                                       # get-vanilla-query-order-key-case
        ('POST', {}, b'', '5da7c1a2acd57cee7505fc6676e4e544621c30862966e37dddb68e92efbe5d6b'),                       # post-vanilla
        ('POST', {'Param1': 'value1'}, b'', '28038455d6de14eafc1f9222cf5aa6f1a96197d7deb8263271d420d138af7f11'),     # post-vanilla-query
    ]
    for method, query, body, signature in cases:
        assert _sign.sigv4(*AWS_KEYS, method=method, path='/', query=query, headers=AWS_HEADERS, body=body, **AWS) == aws_auth(signature)


def test_aws_sigv4_signed_body_case():
    # post-x-www-form-urlencoded with sign_body: content-length/content-type/x-amz-content-sha256 are signed
    body = b'Param1=value1'
    headers = dict(AWS_HEADERS, **{'Content-Type': 'application/x-www-form-urlencoded', 'Content-Length': '13',
                                   'x-amz-content-sha256': _sign.sha256_hex(body)})
    assert headers['x-amz-content-sha256'] == '9095672bbd1f56dfc5b65f3e153adc8731a4a654192329106275f4c7b24d0b6e'
    auth = _sign.sigv4(*AWS_KEYS, method='POST', path='/', query={}, headers=headers, body=body, **AWS)
    assert auth == aws_auth('d3875051da38690788ef43de4db0d8f280229d82040bfac253562e56c3f20e0b',
                            'content-length;content-type;host;x-amz-content-sha256;x-amz-date')
