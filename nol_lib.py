#!/usr/bin/env python3
# vim: set ts=4 sts=4 sw=4 et:

from io import BytesIO
from lxml import etree
from urllib.parse import urlencode, urlparse, parse_qs
import pycurl


class ReadCache:
    def __init__(self, size):
        self.size = size
        self.reset()

    def invalidate(self, addr):
        index = addr % self.size
        if self.cache[index][0] and self.cache[index][1] == addr:
            self.cache[index] = (False, None, None)

    def load(self, addr, miss_func, user_data):
        index = addr % self.size
        if self.cache[index][0] and self.cache[index][1] == addr:
            return self.cache[index][2]
        else:
            value = miss_func(user_data)
            self.cache[index] = (True, addr, value)
            return value

    def reset(self):
        # (valid, addr, value)
        self.cache = [(False, None, None)] * self.size


class NolCrawler:
    # static fields
    base_url = 'https://nol.ntu.edu.tw/nol/coursesearch/search_result.php'
    base_args = {
        'allproced': 'yes',
        'alltime': 'yes',
        'csname': '',
        'cstype': '1'
    }
    doc_encoding = 'big5'
    items_per_page = 15

    # XXX: 臺大課程網有 TLS，但是只支援到 TLSv1.0，所以我們必須手動設定。
    # 這個常數在 pycurl 中並沒有定義，只能手動去 curl/curl.h 找來用。
    ssl_version = 4

    # XXX: 臺大課程網只支援 RC4 和 3DES，不想用 RC4 就得手動指定接受的 cipher。
    # 可是不同的 TLS 函式庫指定方法不太一樣，所以我們自己判斷。
    ssl_library = pycurl.version_info()[5].split('/')[0]
    if ssl_library == 'OpenSSL':
        ssl_cipher = 'DES-CBC3-SHA'
    elif ssl_library == 'GnuTLS':
        ssl_cipher = 'DES-CBC3-SHA'
    elif ssl_library == 'NSS':
        ssl_cipher = 'rsa_3des_sha'
    else:
        raise Exception('Unsupported TLS implementation')


    def __init__(self, semester, cache_size=5):
        self.semester = semester
        self.cache = ReadCache(cache_size)
        self.curl = pycurl.Curl()
        self.curl.setopt(self.curl.SSLVERSION, NolCrawler.ssl_version)
        self.curl.setopt(self.curl.SSL_CIPHER_LIST, NolCrawler.ssl_cipher)
        self.parser = etree.HTMLParser(encoding=NolCrawler.doc_encoding)

    @staticmethod
    def request(curl, data, user_args={}, url_override=None, expected_status=200):
        args = dict(NolCrawler.base_args)
        args.update(user_args)
        if url_override:
            curl.setopt(curl.URL, url_override)
        else:
            curl.setopt(curl.URL, NolCrawler.base_url + '?' + urlencode(args))
        curl.setopt(curl.WRITEDATA, data)
        curl.perform()
        status = curl.getinfo(curl.RESPONSE_CODE)
        if status != expected_status:
            raise Exception(
                'HTTP status {} (not {})'.format(status, expected_status))

    @staticmethod
    def static_request(user_args):
        curl = pycurl.Curl()
        curl.setopt(curl.SSLVERSION, NolCrawler.ssl_version)
        curl.setopt(curl.SSL_CIPHER_LIST, NolCrawler.ssl_cipher)
        data = BytesIO()
        try:
            NolCrawler.request(curl, data, user_args)
        finally:
            curl.close()
        data.seek(0)
        return etree.parse(data, etree.HTMLParser(encoding=NolCrawler.doc_encoding))

    @staticmethod
    def get_semesters():
        html = NolCrawler.static_request({})
        box = html.xpath('//select[@id="select_sem"]')[0]
        opts = map(lambda x: x.get('value'), box.iterchildren(tag='option'))
        return list(opts)

    @staticmethod
    def get_default_semester():
        html = NolCrawler.static_request({})
        opt = html.xpath('//select[@id="select_sem"]/option[@selected]')[0]
        return opt.get('value')

    @staticmethod
    def get_course_count(semester):
        html = NolCrawler.static_request({'current_sem': semester})
        box = html.xpath('//select[@id="select_sem"]')[0]
        count = list(box.getnext())[0]
        return int(count.text)

    @staticmethod
    def get_cache_addr(index):
        return int(index / NolCrawler.items_per_page)

    def get_course(self, index):
        def make_course(row):
            def raw(node):
                return etree.tostring(node, encoding='utf-8').decode('utf-8')

            def safe_str(x):
                return x.strip('\xa0')

            def safe_int(x):
                return -1 if safe_str(x) == '' else int(x)

            def get_link(node):
                children = list(node)
                if len(children) == 0 or children[0].tag != 'a':
                    return None
                return children[0].get('href')

            def get_link_text(node):
                children = list(node)
                if len(children) == 0:
                    return ''
                elif children[0].tag != 'a':
                    return safe_str(node.text)
                return safe_str(children[0].text)

            def get_http_header(header_bytes, header_name):
                for header_line in header_bytes.split(b'\n'):
                    if header_line.startswith(header_name + b':'):
                        return header_line.split(b':', maxsplit=1)[1].strip().decode('ascii')

            cells = list(row)
            course = dict()

            course['ser_no'] = safe_str(cells[0].text)
            course['PRIVATE____dptname'] = safe_str(cells[1].text)
            info_link = get_link(cells[4])
            if info_link:
                course['dpt_code'] = parse_qs(
                    urlparse(info_link).query)['dpt_code'][0]
            else:
                course['dpt_code'] = None

            course['cou_code'] = safe_str(cells[6].text)
            course['credit'] = safe_int(cells[5].text)
            course['co_select'] = safe_int(cells[10].text)
            course['cou_cname'] = get_link_text(cells[4])
            course['tea_cname'] = get_link_text(cells[9])
            course['TODO____clsrom'] = raw(cells[11])
            course['sel_code'] = safe_str(cells[8].text)
            course['TODO____co_gmark'] = raw(cells[13])

            if len(row.xpath('.//img[@src="images/cancel.gif"]')) > 0:
                course['co_chg'] = '停開'
            elif len(row.xpath('.//img[@src="images/chg.gif"]')) > 0:
                course['co_chg'] = '異動'
            else:
                course['co_chg'] = ''

            course['TODO____year'] = None
            course['TODO____comment'] = raw(cells[14])
            course['TODO____tlec'] = raw(cells[11])
            course['klass'] = safe_str(cells[3].text)

            ceiba_link = get_link(cells[15])
            if ceiba_link:
                if ceiba_link.startswith('http://'):
                    ceiba_link = ceiba_link.replace('http', 'https', 1)
                headers = BytesIO()
                self.curl.setopt(self.curl.HEADERFUNCTION, headers.write)
                try:
                    NolCrawler.request(self.curl, BytesIO(),
                        url_override=ceiba_link, expected_status=302)
                except Exception as e:
                    if self.curl.getinfo(self.curl.RESPONSE_CODE) == 404:
                        course['PRIVATE____ceiba'] = None
                    else:
                        raise Exception(str(e))
                else:
                    location = get_http_header(headers.getvalue(), b'Location')
                    if location.startswith('https://ceiba.ntu.edu.tw/login_test.php'):
                        course['PRIVATE____ceiba'] = parse_qs(
                            urlparse(location).query)['csn'][0]
                    elif location.startswith('https://ceiba.ntu.edu.tw/course/'):
                        course['PRIVATE____ceiba'] = location.split('/')[4]
                    else:
                        raise Exception('Unexpected CEIBA URL {}'.format(location))
            else:
                course['PRIVATE____ceiba'] = None

            return course

        def get_page(index):
            page_number = int(index / NolCrawler.items_per_page)
            page_startrec = page_number * NolCrawler.items_per_page
            data = BytesIO()
            args = {
                'current_sem': self.semester,
                'startrec': page_startrec
            }
            NolCrawler.request(self.curl, data, args)
            data.seek(0)
            html = etree.parse(data, etree.HTMLParser(encoding=NolCrawler.doc_encoding))
            rows = html.xpath('/html/body/table[4]/tr[position() > 1]')
            courses = list(map(lambda row: make_course(row), rows))
            return courses

        courses = self.cache.load(NolCrawler.get_cache_addr(index), get_page, index)
        return courses[index % NolCrawler.items_per_page]

    def flush_cache(self, index):
        self.cache.invalidate(NolCrawler.get_cache_addr(index))

    def flush_cache_all(self):
        self.cache.reset()


if __name__ == '__main__':
    print('default: {}'.format(NolCrawler.get_default_semester()))
    print('available: {}'.format(' '.join(NolCrawler.get_semesters())))
