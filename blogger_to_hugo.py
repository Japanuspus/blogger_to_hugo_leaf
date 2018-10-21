#!/usr/bin/env python

import argparse
import io
import logging
import os.path
import re
import sys
import xml.etree.cElementTree as ET

import pypandoc
import requests
import yaml
from urllib.parse import urlparse
from pathlib import Path

from bs4 import BeautifulSoup as bs
from dateutil.parser import parse
from slugify import slugify

logger = logging.getLogger(__name__)

NS = {'atom': 'http://www.w3.org/2005/Atom'}
CATEGORY_KIND = 'http://schemas.google.com/g/2005#kind'
CATEGORY_TAG = 'http://www.blogger.com/atom/ns#'
TERM_POST = 'http://schemas.google.com/blogger/2008/kind#post'


def check_if_file_exists(path, original_path):
    if os.path.exists(path):
        logger.error('File %s -> %s already exists', original_path, path)
        sys.exit(1)


def download_and_save_image(image_src, fullimg_path):
    logger.info('Downloading %s', image_src)
    response = requests.get(image_src)
    if response.status_code != 200:
        raise Exception(f'Can\'t download image {image_src} ; Status code {response.status_code}')

    with io.open(fullimg_path, 'wb') as f:
        f.write(response.content)


def get_post_entries(xml_root):
    result = []
    for entry in xml_root.findall('atom:entry', NS):
        for c in entry.findall('atom:category', NS):
            if (c.attrib['scheme'] == CATEGORY_KIND and
                    c.attrib['term'] == TERM_POST):
                result.append(entry)
                break
    return result


def get_src_resize_if_needed(img_attrs):
    def resize_if_needed(src, size_name, orig_size_name):
        if size_name in img_attrs and orig_size_name in img_attrs:
            size = img_attrs[size_name]
            orig_size = img_attrs[orig_size_name]
            return src.replace(
                '/s{}/'.format(size),
                '/s{}/'.format(orig_size)
            )
        return src

    src = img_attrs['src']

    src = resize_if_needed(src, 'height', 'data-original-height')
    src = resize_if_needed(src, 'width', 'data-original-width')
    return src


def has_identical_extension(a,b):
    return a.split('.')[-1] == b.split('.')[-1]


def replace_images_with_downloaded(html, folder):
    for img in html.find_all('img'):
        if 'src' not in img.attrs:
            continue
        href = get_src_resize_if_needed(img.attrs)
        parent = img.find_parent()

        if parent.name == 'a' and has_identical_extension(href, parent['href']):
            # Assume parent is link to other/larger version: use that instead
            target_tag = parent
            href = parent['href']
        else:
            target_tag = img

        # The blogger/blogspot /s1600-h/ links return an html page
        href = re.sub(r'/(s\d+)-h/', r'/\1/', href)

        name = Path(urlparse(href).path).name
        download_and_save_image(href, folder / name)

        new_img = html.new_tag('img', src=name)
        target_tag.replace_with(new_img)

    return html


def get_post_tags(post):
    result = []
    for c in post.findall('atom:category', NS):
        if c.attrib['scheme'] == CATEGORY_TAG:
            result.append(c.attrib['term'])
    return result


def process_post(post, options, url_map):
    title = post.find('atom:title', NS).text

    logger.info('Starting to process post: %s', title)

    root_folder = Path(options.output_folder)

    # Find original url as "alternate" link -- if this is not present, post is draft
    published_url = next((
            e.get('href') for e in post.findall('atom:link', NS) if e.get('rel')=='alternate')
        , None)


    published_str = post.find('atom:published', NS).text
    published = parse(published_str)
    published_date = '{:04}-{:02}-{:02}'.format(
        published.year, published.month, published.day
    )

    if not published_url:
        slug = slugify(title, to_lower=True)
        post_folder = root_folder / 'draft_posts' / f'{published_date}-{slug}'
        aliases = []
    else:
        p = Path(urlparse(published_url).path)
        # p is an absolute path -- so make sure to eat the leading slash as
        # Path('foo') / '/bar' == Path('/bar')
        slug = p.stem
        post_folder = root_folder / 'post' / f'{published.year:04}' / f'{published_date}-{slug}'
        aliases = [str(p)]
        new_url = options.new_root+str(p.parent)+'/'+p.stem
        url_map.append((published_url, new_url))
    post_folder.mkdir(parents=True)

    content = post.find('atom:content', NS).text
    author_name = post.find('atom:author', NS).find('atom:name', NS).text
    tags = get_post_tags(post)

    html = bs(content, 'html.parser')
    html = replace_images_with_downloaded(html, post_folder)
    mkd = pypandoc.convert_text(html, 'markdown_strict', format='html').strip()
    metadata_dict = {
        'title': title,
        'slug' : slug,
        'published': published_date, 
        'author': author_name,
        'tags': tags,
    }
    if aliases and options.front_alias:
        # static site cannot handle both /yyyy/mm/slug.html and yyyy/mm/slug/
        # use a clever 404 instead
        metadata_dict['aliases'] = aliases    
    front_matter = yaml.dump(metadata_dict, default_flow_style=False).strip()

    (post_folder / 'index.md').write_text(f'---\n{front_matter}\n---\n{mkd}')

    logger.info(f'Saving to {post_folder}')


def check_folder_path(folder_path):
    if os.path.exists(folder_path):
        raise argparse.ArgumentTypeError(
            'Output path "{}" already exists'.format(folder_path)
        )
    return folder_path


def check_blogger_xml(file_path):
    if not os.path.exists(file_path):
        raise argparse.ArgumentTypeError(
            'Such file "{}" does not exist'.format(file_path)
        )
    return file_path


def parser_arguments():
    parser = argparse.ArgumentParser()


    parser.add_argument(
        '--num_posts', default=None, type=int,
        help='Number of posts to process. Mostly for debug')

    parser.add_argument(
        'blogger_file',
        metavar='BLOGGER_XML_FILE',
        help='Path to blogger xml file',
        type=check_blogger_xml
    )
    parser.add_argument(
        'output_folder',
        metavar='OUTPUT_FOLDER',
        help='Output folder path',
        type=check_folder_path,
    )
    parser.add_argument('--new_root', default='', help='root to use when constructinn url map. No slash at end.')
    parser.add_argument('--front_alias', default=False, action='store_true', help='Include alias to previous URL at front')

    return parser.parse_args()


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    options = parser_arguments()

    try:
        xml_tree = ET.parse(options.blogger_file)
    except ET.ParseError:
        raise Exception(
            f'Can not parse "{options.blogger_file}". Check if it is actually '
            +'exported blogger\'s xml file')
        
    xml_root = xml_tree.getroot()

    posts = get_post_entries(xml_root)

    if options.num_posts is not None:
        posts = posts[:options.num_posts]

    url_map = []
    for post in posts:
        process_post(post, options, url_map)

    url_map_file = Path(options.output_folder) / 'url_map.csv'
    logger.info(f'Writing url map to {url_map_file}')
    url_map_file.write_text(''.join(f'{o},{n},\n' for o,n in url_map))

if __name__ == "__main__":
    main()
