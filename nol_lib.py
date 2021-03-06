#!/usr/bin/env python3
# vim: set ts=4 sts=4 sw=4 et:

from io import BytesIO
from lxml import etree
from urllib.parse import urlencode, urlparse, parse_qs
import pycurl
import re


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
    # 這個常數只有在新版的 pycurl 才有定義，所以使用前得先檢查。
    # https://bugzilla.redhat.com/show_bug.cgi?id=1260408
    # https://github.com/pycurl/pycurl/commit/83ccaa2
    if hasattr(pycurl.Curl(), 'SSLVERSION_TLSv1_0'):
        ssl_version = pycurl.Curl().SSLVERSION_TLSv1_0
    else:
        ssl_version = 4

    # XXX: 臺大課程網只支援 RC4 和 3DES，不想用 RC4 就得手動指定接受的 cipher。
    # 可是不同的 TLS 函式庫指定方法不太一樣，所以我們自己判斷。
    ssl_library = pycurl.version_info()[5].split('/')[0]
    if ssl_library == 'OpenSSL':
        ssl_cipher_nol = 'DES-CBC3-SHA'
        ssl_cipher_ceiba = 'ECDHE-RSA-AES128-GCM-SHA256'
    elif ssl_library == 'GnuTLS':
        ssl_cipher_nol = 'DES-CBC3-SHA'
        ssl_cipher_ceiba = 'ECDHE-RSA-AES128-GCM-SHA256'
    elif ssl_library == 'NSS':
        ssl_cipher_nol = 'rsa_3des_sha'
        ssl_cipher_ceiba = 'ecdhe_rsa_aes_128_gcm_sha_256'
    else:
        raise Exception('Unsupported TLS implementation')


    def __init__(self, semester, ceiba=True, debug=False, cache_size=5):
        self.semester = semester
        self.ceiba = ceiba
        self.cache = ReadCache(cache_size)
        self.curl = pycurl.Curl()
        self.curl.setopt(self.curl.SSLVERSION, NolCrawler.ssl_version)
        self.curl.setopt(pycurl.VERBOSE, 1 if debug else 0)
        self.parser = etree.HTMLParser(encoding=NolCrawler.doc_encoding)

    @staticmethod
    def request(curl, data, cipher, user_args={}, url_override=None, expected_status=200):
        args = dict(NolCrawler.base_args)
        args.update(user_args)
        if url_override:
            curl.setopt(curl.URL, url_override)
        else:
            curl.setopt(curl.URL, NolCrawler.base_url + '?' + urlencode(args))
        curl.setopt(curl.SSL_CIPHER_LIST, cipher)
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
        data = BytesIO()
        try:
            NolCrawler.request(curl, data, NolCrawler.ssl_cipher_nol, user_args)
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
                return '' if x is None else x.strip('\xa0')

            def safe_int(x):
                return 0 if safe_str(x) == '' else int(x)

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
            sem_year, sem_index = map(int, self.semester.split('-'))

            course['ser_no'] = safe_str(cells[0].text)
            course['PRIVATE____dptname'] = safe_str(cells[1].text)
            info_link = get_link(cells[4])
            if info_link:
                course['dpt_code'] = parse_qs(
                    urlparse(info_link).query)['dpt_code'][0]
            else:
                course['dpt_code'] = None

            course['cou_code'] = safe_str(cells[7].text)
            if sem_year >= 106 or (sem_year == 105 and sem_index >= 2):
                course['credit'] = float(cells[6].text)
            else:
                course['credit'] = safe_int(cells[6].text)

            course['co_select'] = safe_int(cells[11].text)
            course['cou_cname'] = get_link_text(cells[4])
            course['tea_cname'] = get_link_text(cells[10])
            tea_link = get_link(cells[10])
            if tea_link:
                tea_link_parsed = parse_qs(urlparse(tea_link).query)
                assert tea_link_parsed['op'][0] == 's2'
                course['PRIVATE____teaid'] = tea_link_parsed['td'][0]
            else:
                course['PRIVATE____teaid'] = None

            course['PRIVATE____video'] = get_link(cells[5])

            def read_time_clsrom(text):
                # 開頭如果有第2,3,4,5,6 週之類的東西直接先拿掉
                if text.startswith('第'):
                    prefix_begin = 1
                    prefix_end = text.find('週')
                    assert prefix_end > prefix_begin
                    for char in text[prefix_begin:prefix_end]:
                        assert char in list('0123456789') + [' ', ',']
                    text = text[prefix_end + 1:]
                text_len = len(text)
                result = list()
                state = 3 # 一開始就有可能出現多餘括號
                brackets = 0
                if sem_year >= 104:
                    is_104_or_later = True
                    time_list = list('0123456789') + ['10'] + list('ABCD')
                else:
                    is_104_or_later = False
                    time_list = list('01234@56789ABCD')
                time_dash = False # 可能會有像是 1-@ (等同 1234@) 這種表示法
                day = clsrom = ''
                unexpected_clsrom = ''
                time = []
                uncommitted_time = ''
                for char in text:
                    if char.isspace():
                        continue
                    if brackets == 0 and state == 3 and char != '(':
                        state = 0
                    if state == 0: # day
                        assert char in '一二三四五六日'
                        day = char
                        state += 1
                    elif state == 1: # time
                        # 104 學年度以後節課可能出現 10，所以一定都會用逗號分隔
                        if is_104_or_later:
                            if char == ',' or char == '(':
                                if uncommitted_time != '':
                                    assert uncommitted_time in time_list
                                    time.append(uncommitted_time)
                                    uncommitted_time = ''
                                if char == '(':
                                    brackets += 1
                                    state += 1
                                    continue
                            else:
                                uncommitted_time += char
                        # 104 學年度以前的資料雖也改用逗號分隔，但是常常分得不
                        # 正確，造成像是 1,-,@、8,9,1,0、9,,,A 這類錯誤。因此
                        # 我們繼續使用舊的作法，忽略逗號。
                        else:
                            if char == ',':
                                continue
                            if char == '-':
                                time_dash = True
                                assert uncommitted_time == ''
                                continue
                            if char == '*':
                                time.append('*')
                                assert uncommitted_time == ''
                                continue
                            if char == '(':
                                brackets += 1
                                state += 1
                                uncommitted_time = ''
                                continue
                            if char in time_list:
                                if len(time) > 0:
                                    time_prev = time_list.index(time[-1])
                                    if uncommitted_time == '':
                                        time_this = time_list.index(char)
                                    else:
                                        # 目前已知會出現兩位數的只有 10
                                        assert uncommitted_time + char == '10'
                                        # 重新對應回 A
                                        time_this = time_list.index('A')
                                        uncommitted_time = ''
                                    # 檢查我們是不是遇到兩位數的。不過要注意時
                                    # 間有可能沒排序，所以我們還是假設只可能出
                                    # 現 10。
                                    if time_this <= time_prev and char == '1':
                                        uncommitted_time += char
                                    else:
                                        if time_dash:
                                            assert len(time) == 1
                                            time_this += 1
                                            time = time_list[time_prev:time_this]
                                        else:
                                            time.append(time_list[time_this])
                                else:
                                    time.append(char)
                            else:
                                assert False
                    elif state == 2: # clsrom
                        if char == '(':
                            brackets += 1
                        elif char == ')':
                            brackets -= 1
                        if brackets > 0:
                            clsrom += char
                        elif brackets == 0:
                            result.append((day, time, clsrom))
                            day = clsrom = ''
                            time = []
                            state += 1
                        else:
                            assert False
                    elif state == 3: # 多餘括號裡的資料先保存起來
                        if char == '(':
                            if brackets > 0:
                                unexpected_clsrom += char
                            brackets += 1
                        elif char == ')':
                            brackets -= 1
                            if brackets > 0:
                                unexpected_clsrom += char
                        else:
                            if brackets > 0:
                                unexpected_clsrom += char
                    else:
                        assert False
                # 括號可能沒有配對，這時候我們要直接先送出結果，不經過 assert
                if clsrom.endswith(')') and brackets > 0:
                    result.append((day, time, clsrom))
                    return result
                # 可能沒有時間，只有教室，我們手動填空直接回傳
                if day == '' and time == [] and clsrom == '' and \
                    unexpected_clsrom != '' and len(result) == 0:
                    result.append(('', '', unexpected_clsrom))
                    return result
                # 如果教室全部都是「請洽系所辦」，那就從前面多出來的挖
                if list(map(lambda r: r[2], result)) == ['請洽系所辦'] * len(result):
                    if unexpected_clsrom != '':
                        for i in range(0, len(result)):
                            result[i] = (result[i][0], result[i][1], unexpected_clsrom)
                assert day == '' and time == [] and clsrom == '' and brackets == 0
                return result

            time_clsrom_text = safe_str(''.join(cells[12].itertext()))
            course['time_clsrom'] = read_time_clsrom(time_clsrom_text)
            course['PRIVATE____time_clsrom'] = time_clsrom_text

            course['sel_code'] = safe_str(cells[9].text)
            for text in cells[14].itertext():
                if text is not None:
                    co_gmark = re.search('A[1-8]+\**', text)
                    if co_gmark is not None:
                        course['co_gmark'] = safe_str(co_gmark.group(0))
                    else:
                        course['co_gmark'] = None

            if len(row.xpath('.//img[@src="images/cancel.gif"]')) > 0:
                course['co_chg'] = '停開'
            elif len(row.xpath('.//img[@src="images/add.gif"]')) > 0:
                course['co_chg'] = '加開'
            elif len(row.xpath('.//img[@src="images/chg.gif"]')) > 0:
                course['co_chg'] = '異動'
            else:
                assert len(row.xpath('.//img')) == 0 or \
                    len(row.xpath('.//img[@src="images/courseweb.gif"]')) > 0
                course['co_chg'] = None
            assert 'co_chg' in course.keys()

            course['comment'] = safe_str(''.join(cells[15].itertext()))
            course['klass'] = safe_str(cells[3].text)

            ceiba_link = get_link(cells[16])
            if self.ceiba and ceiba_link:
                if ceiba_link.startswith('http://'):
                    ceiba_link = ceiba_link.replace('http', 'https', 1)
                headers = BytesIO()
                self.curl.setopt(self.curl.HEADERFUNCTION, headers.write)
                try:
                    NolCrawler.request(self.curl, BytesIO(),
                        NolCrawler.ssl_cipher_ceiba,
                        url_override=ceiba_link, expected_status=302)
                except Exception as e:
                    if self.curl.getinfo(self.curl.RESPONSE_CODE) == 404 or \
                        self.curl.getinfo(self.curl.RESPONSE_CODE) == 200:
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
            assert 'PRIVATE____ceiba' in course.keys()

            return course

        def get_page(index):
            page_number = int(index / NolCrawler.items_per_page)
            page_startrec = page_number * NolCrawler.items_per_page
            data = BytesIO()
            args = {
                'current_sem': self.semester,
                'startrec': page_startrec
            }
            NolCrawler.request(self.curl, data, NolCrawler.ssl_cipher_nol, args)
            data.seek(0)
            html = etree.parse(data, etree.HTMLParser(encoding=NolCrawler.doc_encoding))
            rows = html.xpath('/html/body/table[4]/tr[position() > 1]')
            if len(rows) == 0 and len(html.xpath('/html/body/table')) == 0:
                raise Exception('NOL website down')
            courses = list(map(make_course, rows))
            # 有些頁面可能有缺項，但我們還是得補滿到剛好一頁
            missing_count = NolCrawler.items_per_page - len(courses)
            return courses + [ {'not_found': True} ] * missing_count

        if index < 0:
            return None
        courses = self.cache.load(NolCrawler.get_cache_addr(index), get_page, index)
        return courses[index % NolCrawler.items_per_page]

    def flush_cache(self, index):
        self.cache.invalidate(NolCrawler.get_cache_addr(index))

    def flush_cache_all(self):
        self.cache.reset()


if __name__ == '__main__':
    print('default: {}'.format(NolCrawler.get_default_semester()))
    print('available: {}'.format(' '.join(NolCrawler.get_semesters())))
