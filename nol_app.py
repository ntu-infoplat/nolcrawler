#!/usr/bin/env python3
# vim: set ts=4 sts=4 sw=4 et:

from nol_lib import NolCrawler
from pprint import pprint
from json import dumps
from sys import argv, stderr

def update_progress(now, total):
    percent = now / total * 100
    hashes = int(percent / 2)
    blanks = 50 - hashes
    print('\r({:5d}/{:5d}) [{}{}] {:6.2f}%'.format(
        now, total, '#' * hashes, ' ' * blanks, percent), end='', file=stderr)
    stderr.flush()

if __name__ == '__main__':
    try:
        argv.index('--help')
        print('Usage: {} semester start_index'.format(argv[0]))
        exit(0)
    except ValueError:
        pass

    semester = argv[1] if len(argv) >= 2 else NolCrawler.get_default_semester()
    start_index = int(argv[2]) if len(argv) >= 3 else 0
    pretty = True if len(argv) >= 4 else False
    crawler = NolCrawler(semester)
    count = NolCrawler.get_course_count(semester)

    if count == 0:
        print('No such semester', file=stderr)
        exit(1)

    for index in range(start_index, count):
        if index % NolCrawler.items_per_page == 0:
            update_progress(index, count)
        first_error = True
        while True:
            try:
                course = crawler.get_course(index)
                break
            except Exception as e:
                if first_error:
                    print('', file=stderr)
                first_error = False
                print('Error at {}: {}'.format(index, str(e)), file=stderr)
        course.update({'.__index__.': index})
        if pretty:
            pprint(course)
        else:
            print(dumps(course, ensure_ascii=False, sort_keys=True))
    update_progress(count, count)
    print('', file=stderr)
