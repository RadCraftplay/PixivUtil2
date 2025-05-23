# -*- coding: utf-8 -*-
import datetime
import gc
import os
import re
import sys
import shutil
import time
import traceback
import pathlib
from urllib.error import URLError

from colorama import Fore, Style

import datetime_z
import PixivBrowserFactory
import PixivConstant
import PixivDownloadHandler
import PixivHelper
from PixivDBManager import PixivDBManager
from PixivException import PixivException

__re_manga_page = re.compile(r'(\d+(_big)?_p\d+)')


def process_image(caller,
                  config,
                  artist=None,
                  image_id=None,
                  user_dir='',
                  bookmark=False,
                  search_tags='',
                  title_prefix="",
                  bookmark_count=-1,
                  image_response_count=-1,
                  notifier=None,
                  useblacklist=True,
                  reencoding=False,
                  manga_series_order=-1,
                  manga_series_parent=None,
                  ui_prefix="",
                  is_unlisted=False) -> int:
    # caller function/method
    # TODO: ideally to be removed or passed as argument
    db: PixivDBManager = caller.__dbManager__

    if notifier is None:
        notifier = PixivHelper.dummy_notifier

    # override the config source if job_option is give for filename formats
    extension_filter = None
    if hasattr(config, "extensionFilter"):
        extension_filter = config.extensionFilter

    parse_medium_page = None
    image = None
    result = None
    if not is_unlisted:
        # https://www.pixiv.net/en/artworks/76656661
        referer = f"https://www.pixiv.net/artworks/{image_id}"
    else:
        # https://www.pixiv.net/artworks/unlisted/SbliQHtJS5MMu3elqDFZ
        referer = f"https://www.pixiv.net/artworks/unlisted/{image_id}"
    filename = f'no-filename-{image_id}.tmp'

    try:
        msg = ui_prefix + Fore.YELLOW + Style.NORMAL + f'Processing Image Id: {image_id}' + Style.RESET_ALL
        PixivHelper.print_and_log(None, msg)
        notifier(type="IMAGE", message=msg)

        # check if already downloaded. images won't be downloaded twice - needed in process_image to catch any download
        r = db.selectImageByImageId(image_id, cols='save_name')
        exists = False
        in_db = False
        if r is not None:
            exists = db.cleanupFileExists(r[0])
            in_db = True

        # skip if already recorded in db and alwaysCheckFileSize is disabled and overwrite is disabled.
        if in_db and not config.alwaysCheckFileSize and not config.overwrite and not reencoding:
            PixivHelper.print_and_log(None, f'Already downloaded in DB: {image_id}')
            gc.collect()
            return PixivConstant.PIXIVUTIL_SKIP_DUPLICATE_NO_WAIT

        # get the medium page
        try:
            (image, parse_medium_page) = PixivBrowserFactory.getBrowser().getImagePage(image_id=image_id,
                                                                                       parent=artist,
                                                                                       from_bookmark=bookmark,
                                                                                       bookmark_count=bookmark_count,
                                                                                       manga_series_order=manga_series_order,
                                                                                       manga_series_parent=manga_series_parent,
                                                                                       is_unlisted=is_unlisted)
            if len(title_prefix) > 0:
                caller.set_console_title(f"{title_prefix} ImageId: {image.imageId}")
            else:
                assert (image.artist is not None)
                caller.set_console_title(f"MemberId: {image.artist.artistId} ImageId: {image.imageId}")

        except PixivException as ex:
            caller.ERROR_CODE = ex.errorCode
            caller.__errorList.append(dict(type="Image", id=str(image_id), message=ex.message, exception=ex))
            if ex.errorCode == PixivException.UNKNOWN_IMAGE_ERROR:
                PixivHelper.print_and_log('error', ex.message)
            elif ex.errorCode == PixivException.SERVER_ERROR:
                PixivHelper.print_and_log('error', f'Giving up image_id (medium): {image_id}')
            elif ex.errorCode > 2000:
                PixivHelper.print_and_log('error', f'Image Error for {image_id}: {ex.message}')
            if parse_medium_page is not None:
                dump_filename = f'Error medium page for image {image_id}.html'
                PixivHelper.dump_html(dump_filename, parse_medium_page)
                PixivHelper.print_and_log('error', f'Dumping html to: {dump_filename}')
            else:
                PixivHelper.print_and_log('error', f'Image ID ({image_id}): {ex}')
            PixivHelper.print_and_log('error', f'Stack Trace: {sys.exc_info()}')
            return PixivConstant.PIXIVUTIL_NOT_OK
        except Exception as ex:
            PixivHelper.print_and_log('error', f'Image ID ({image_id}): {ex}')
            if parse_medium_page is not None:
                dump_filename = f'Error medium page for image {image_id}.html'
                PixivHelper.dump_html(dump_filename, parse_medium_page)
                PixivHelper.print_and_log('error', f'Dumping html to: {dump_filename}')
            PixivHelper.print_and_log('error', f'Stack Trace: {sys.exc_info()}')
            exc_type, exc_value, exc_traceback = sys.exc_info()
            traceback.print_exception(exc_type, exc_value, exc_traceback)
            return PixivConstant.PIXIVUTIL_NOT_OK

        download_image_flag = True

        # feature #1189 AI filtering
        if config.aiDisplayFewer and image.ai_type == 2:
            PixivHelper.print_and_log('warn', f'Skipping image_id: {image_id} – blacklisted due to aiDisplayFewer is set to True and aiType = {image.ai_type}.')
            download_image_flag = False
            result = PixivConstant.PIXIVUTIL_SKIP_BLACKLIST

        # date validation and blacklist tag validation
        if config.dateDiff > 0:
            if image.worksDateDateTime is not None and image.worksDateDateTime != datetime.datetime.fromordinal(1).replace(tzinfo=datetime_z.utc):
                if image.worksDateDateTime < (datetime.datetime.today() - datetime.timedelta(config.dateDiff)).replace(tzinfo=datetime_z.utc):
                    PixivHelper.print_and_log('warn', f'Skipping image_id: {image_id} – it\'s older than: {config.dateDiff} day(s).')
                    download_image_flag = False
                    result = PixivConstant.PIXIVUTIL_SKIP_OLDER

        if useblacklist:
            if config.useBlacklistMembers and download_image_flag:
                if image.originalArtist is not None and str(image.originalArtist.artistId) in caller.__blacklistMembers:
                    PixivHelper.print_and_log('warn', f'Skipping image_id: {image_id} – blacklisted member id: {image.originalArtist.artistId}')
                    download_image_flag = False
                    result = PixivConstant.PIXIVUTIL_SKIP_BLACKLIST

            if config.useBlacklistTags and download_image_flag:
                for item in caller.__blacklistTags:
                    if item in image.imageTags:
                        PixivHelper.print_and_log('warn', f'Skipping image_id: {image_id} – blacklisted tag: {item}')
                        download_image_flag = False
                        result = PixivConstant.PIXIVUTIL_SKIP_BLACKLIST
                        break

            # Issue #439
            if config.r18Type == 1 and download_image_flag:
                # only download R18 if r18Type = 1
                if 'R-18G' in (tag.upper() for tag in image.imageTags):
                    PixivHelper.print_and_log('warn', f'Skipping image_id: {image_id} because it has R-18G tag.')
                    download_image_flag = False
                    result = PixivConstant.PIXIVUTIL_SKIP_BLACKLIST
            elif config.r18Type == 2 and download_image_flag:
                # only download R18G if r18Type = 2
                if 'R-18' in (tag.upper() for tag in image.imageTags):
                    PixivHelper.print_and_log('warn', f'Skipping image_id: {image_id} because it has R-18 tag.')
                    download_image_flag = False
                    result = PixivConstant.PIXIVUTIL_SKIP_BLACKLIST

            if config.useBlacklistTitles and download_image_flag:
                if config.useBlacklistTitlesRegex:
                    for item in caller.__blacklistTitles:
                        if re.search(rf"{item}", image.imageTitle):
                            PixivHelper.print_and_log('warn', f'Skipping image_id: {image_id} – Title matched: {item}')
                            download_image_flag = False
                            result = PixivConstant.PIXIVUTIL_SKIP_BLACKLIST
                            break
                else:
                    for item in caller.__blacklistTitles:
                        if item in image.imageTitle:
                            PixivHelper.print_and_log('warn', f'Skipping image_id: {image_id} – Title contained: {item}')
                            download_image_flag = False
                            result = PixivConstant.PIXIVUTIL_SKIP_BLACKLIST
                            break

        # Issue #726
        if extension_filter is not None and len(extension_filter) > 0:
            for url in image.imageUrls:
                ext = PixivHelper.get_extension_from_url(url)

                # add alias for ugoira
                if "ugoira" in extension_filter:
                    extension_filter = f"{extension_filter}|zip"

                if re.search(extension_filter, ext) is None:
                    download_image_flag = False
                    PixivHelper.print_and_log('warn', f'Skipping image_id: {image_id} - url is not in the filter: {extension_filter} => {url}')
                    break

        # issue #1027 filter by bookmark count
        if bookmark_count is not None and int(bookmark_count) > -1 and int(image.bookmark_count) < int(bookmark_count):
            download_image_flag = False
            PixivHelper.print_and_log('warn', f'Skipping image_id: {image_id} - post bookmark count {image.bookmark_count} is less than: {bookmark_count}')

        if download_image_flag:
            if artist is None and image.artist is not None:
                PixivHelper.print_and_log(None, f'{Fore.LIGHTCYAN_EX}{"Member Name":14}:{Style.RESET_ALL} {image.artist.artistName}')
                PixivHelper.print_and_log(None, f'{Fore.LIGHTCYAN_EX}{"Member Avatar":14}:{Style.RESET_ALL} {image.artist.artistAvatar}')
                PixivHelper.print_and_log(None, f'{Fore.LIGHTCYAN_EX}{"Member Token":14}:{Style.RESET_ALL} {image.artist.artistToken}')
                PixivHelper.print_and_log(None, f'{Fore.LIGHTCYAN_EX}{"Member Backgrd":14}:{Style.RESET_ALL} {image.artist.artistBackground}')

            PixivHelper.print_and_log(None, f"{Fore.LIGHTCYAN_EX}{'Title':10}:{Style.RESET_ALL} {image.imageTitle}")
            if len(image.translated_work_title) > 0:
                PixivHelper.print_and_log(None, f"{Fore.LIGHTCYAN_EX}{'TL-ed Title':10}: {image.translated_work_title}")
            tags_str = ', '.join(image.imageTags).replace("AI-generated", f"{Fore.LIGHTYELLOW_EX}AI-generated{Style.RESET_ALL}")
            PixivHelper.print_and_log(None, f"{Fore.LIGHTCYAN_EX}{'Tags':10}:{Style.RESET_ALL} {tags_str}")
            PixivHelper.print_and_log(None, f"{Fore.LIGHTCYAN_EX}{'Date':10}:{Style.RESET_ALL} {image.worksDateDateTime}")
            PixivHelper.print_and_log(None, f"{Fore.LIGHTCYAN_EX}{'Mode':10}:{Style.RESET_ALL} {image.imageMode}")
            PixivHelper.print_and_log(None, f"{Fore.LIGHTCYAN_EX}{'Bookmarks':10}:{Style.RESET_ALL} {image.bookmark_count}")

            if config.useSuppressTags:
                for item in caller.__suppressTags:
                    if item in image.imageTags:
                        image.imageTags.remove(item)

            # get manga page
            if image.imageMode == 'manga':
                PixivHelper.print_and_log(None, f"{Fore.LIGHTCYAN_EX}{'Pages':10}:{Style.RESET_ALL} {image.imageCount}")

            if user_dir == '':  # Yavos: use config-options
                target_dir = config.rootDirectory
            else:  # Yavos: use filename from list
                target_dir = user_dir

            result = PixivConstant.PIXIVUTIL_OK
            manga_files = list()
            page = 0

            # Issue #639
            source_urls = image.imageUrls
            if config.downloadResized:
                source_urls = image.imageResizedUrls

            # debugging purpose, to avoid actual download
            if caller.DEBUG_SKIP_DOWNLOAD_IMAGE:
                return PixivConstant.PIXIVUTIL_OK

            current_img = 1
            total = len(source_urls)
            for img in source_urls:
                prefix = f"{Fore.CYAN}[{current_img}/{total}]{Style.RESET_ALL} "
                PixivHelper.print_and_log(None, f'{prefix}Image URL : {img}')
                url = os.path.basename(img)
                # split_url = url.split('.')
                # if split_url[0].startswith(str(image_id)):
                filename_format = config.filenameFormat
                if image.imageMode == 'manga':
                    filename_format = config.filenameMangaFormat

                filename = PixivHelper.make_filename(filename_format,
                                                        image,
                                                        tagsSeparator=config.tagsSeparator,
                                                        tagsLimit=config.tagsLimit,
                                                        fileUrl=url,
                                                        bookmark=bookmark,
                                                        searchTags=search_tags,
                                                        useTranslatedTag=config.useTranslatedTag,
                                                        tagTranslationLocale=config.tagTranslationLocale)
                filename = PixivHelper.sanitize_filename(filename, target_dir)

                if image.imageMode == 'manga' and config.createMangaDir:
                    manga_page = __re_manga_page.findall(filename)
                    if len(manga_page) > 0:
                        splitted_filename = filename.split(manga_page[0][0], 1)
                        splitted_manga_page = manga_page[0][0].split("_p", 1)
                        # filename = splitted_filename[0] + splitted_manga_page[0] + os.sep + "_p" + splitted_manga_page[1] + splitted_filename[1]
                        filename = f"{splitted_filename[0]}{splitted_manga_page[0]}{os.sep}_p{splitted_manga_page[1]}{splitted_filename[1]}"

                PixivHelper.print_and_log('info', f'{prefix}Filename  : {filename}')

                result = PixivConstant.PIXIVUTIL_NOT_OK
                try:
                    (result, filename) = PixivDownloadHandler.download_image(caller,
                                                                                img,
                                                                                filename,
                                                                                referer,
                                                                                config.overwrite,
                                                                                config.retry,
                                                                                config.backupOldFile,
                                                                                image,
                                                                                page,
                                                                                notifier)

                    if result == PixivConstant.PIXIVUTIL_NOT_OK:
                        PixivHelper.print_and_log('error', f'Image url not found/failed to download: {image.imageId}')
                    elif result == PixivConstant.PIXIVUTIL_KEYBOARD_INTERRUPT:
                        raise KeyboardInterrupt()

                    manga_files.append((image_id, page, filename))
                    page = page + 1

                except URLError:
                    PixivHelper.print_and_log('error', f'Error when download_image(), giving up url: {img}')
                PixivHelper.print_and_log(None, '')

                # XMP image info per images
                if config.writeImageXMPPerImage:
                    filename_info_format = config.filenameInfoFormat or config.filenameFormat
                    # Issue #575
                    if image.imageMode == 'manga':
                        filename_info_format = config.filenameMangaInfoFormat or config.filenameMangaFormat or filename_info_format
                    # If we are creating an ugoira, we need to create side-car metadata for each converted file.
                    if image.imageMode == 'ugoira_view':
                        def get_info_filename(extension):
                            fileUrl = os.path.splitext(url)[0] + "." + extension
                            info_filename = PixivHelper.make_filename(filename_info_format,
                                            image,
                                            tagsSeparator=config.tagsSeparator,
                                            tagsLimit=config.tagsLimit,
                                            fileUrl=fileUrl,
                                            appendExtension=False,
                                            bookmark=bookmark,
                                            searchTags=search_tags,
                                            useTranslatedTag=config.useTranslatedTag,
                                            tagTranslationLocale=config.tagTranslationLocale)
                            return PixivHelper.sanitize_filename(info_filename + ".xmp", target_dir)
                        if config.createGif:
                            info_filename = get_info_filename("gif")
                            image.WriteXMP(info_filename, config.useTranslatedTag, config.tagTranslationLocale)
                        if config.createApng:
                            info_filename = get_info_filename("apng")
                            image.WriteXMP(info_filename, config.useTranslatedTag, config.tagTranslationLocale)
                        if config.createAvif:
                            info_filename = get_info_filename("avif")
                            image.WriteXMP(info_filename, config.useTranslatedTag, config.tagTranslationLocale)
                        if config.createWebm:
                            info_filename = get_info_filename("webm")
                            image.WriteXMP(info_filename, config.useTranslatedTag, config.tagTranslationLocale)
                        if config.createWebp:
                            info_filename = get_info_filename("webp")
                            image.WriteXMP(info_filename, config.useTranslatedTag, config.tagTranslationLocale)
                        if config.createMkv:
                            info_filename = get_info_filename("mkv")
                            image.WriteXMP(info_filename, config.useTranslatedTag, config.tagTranslationLocale)
                        if not config.deleteZipFile:
                            info_filename = get_info_filename("zip")
                            image.WriteXMP(info_filename, config.useTranslatedTag, config.tagTranslationLocale)
                        if not config.deleteUgoira:
                            info_filename = get_info_filename("ugoira")
                            image.WriteXMP(info_filename, config.useTranslatedTag, config.tagTranslationLocale)
                    else:
                        info_filename = PixivHelper.make_filename(filename_info_format,
                                                                    image,
                                                                    tagsSeparator=config.tagsSeparator,
                                                                    tagsLimit=config.tagsLimit,
                                                                    fileUrl=url,
                                                                    appendExtension=False,
                                                                    bookmark=bookmark,
                                                                    searchTags=search_tags,
                                                                    useTranslatedTag=config.useTranslatedTag,
                                                                    tagTranslationLocale=config.tagTranslationLocale)
                        info_filename = PixivHelper.sanitize_filename(info_filename + ".xmp", target_dir)
                        image.WriteXMP(info_filename, config.useTranslatedTag, config.tagTranslationLocale)
                current_img = current_img + 1

            if config.writeImageInfo or config.writeImageJSON or config.writeImageXMP:
                filename_info_format = config.filenameInfoFormat or config.filenameFormat
                # Issue #575
                if image.imageMode == 'manga':
                    filename_info_format = config.filenameMangaInfoFormat or config.filenameMangaFormat or filename_info_format
                info_filename = PixivHelper.make_filename(filename_info_format,
                                                          image,
                                                          tagsSeparator=config.tagsSeparator,
                                                          tagsLimit=config.tagsLimit,
                                                          fileUrl=url,
                                                          appendExtension=False,
                                                          bookmark=bookmark,
                                                          searchTags=search_tags,
                                                          useTranslatedTag=config.useTranslatedTag,
                                                          tagTranslationLocale=config.tagTranslationLocale)
                if image.imageMode == 'manga':
                    # trim _pXXX for manga
                    info_filename = re.sub(r'_p?\d+$', '', info_filename)
                info_filename = PixivHelper.sanitize_filename(info_filename + ".infoext", target_dir)
                if config.writeImageInfo:
                    image.WriteInfo(info_filename[:-8] + ".txt")
                if config.writeImageJSON:
                    image.WriteJSON(info_filename[:-8] + ".json", config.RawJSONFilter, config.useTranslatedTag, config.tagTranslationLocale)
                if config.includeSeriesJSON and image.seriesNavData and image.seriesNavData['seriesId'] not in caller.__seriesDownloaded:
                    json_filename = PixivHelper.make_filename(config.filenameSeriesJSON, image, fileUrl=url, appendExtension=False)
                    if image.imageMode == 'manga':
                        # trim _pXXX for manga
                        json_filename = re.sub(r'_p?\d+$', '', json_filename)
                    json_filename = PixivHelper.sanitize_filename(json_filename + ".json", target_dir)
                    image.WriteSeriesData(image.seriesNavData['seriesId'], caller.__seriesDownloaded, json_filename)
                if config.writeImageXMP and not config.writeImageXMPPerImage:
                    image.WriteXMP(info_filename[:-8] + ".xmp", config.useTranslatedTag, config.tagTranslationLocale)

            if image.imageMode == 'ugoira_view':
                if config.writeUgoiraInfo:
                    image.WriteUgoiraData(filename + ".js")
                # Handle #451
                if config.createUgoira and (result in (PixivConstant.PIXIVUTIL_OK, PixivConstant.PIXIVUTIL_SKIP_DUPLICATE)):
                    PixivDownloadHandler.handle_ugoira(image, filename, config, notifier)

            if config.writeUrlInDescription:
                PixivHelper.write_url_in_description(image, config.urlBlacklistRegex, config.urlDumpFilename)

        if in_db and not exists:
            result = PixivConstant.PIXIVUTIL_CHECK_DOWNLOAD  # There was something in the database which had not been downloaded

        # Only save to db if all images is downloaded completely
        if result in (PixivConstant.PIXIVUTIL_OK,
                      PixivConstant.PIXIVUTIL_SKIP_DUPLICATE,
                      PixivConstant.PIXIVUTIL_SKIP_LOCAL_LARGER):
            caption = image.imageCaption if config.autoAddCaption else ""
            try:
                assert (image.artist is not None)
                db.insertImage(image.artist.artistId, image.imageId, image.imageMode, caption=caption)
            except BaseException:
                PixivHelper.print_and_log('error', f'Failed to insert image id:{image.imageId} to DB')

            db.updateImage(image.imageId, image.imageTitle, filename, image.imageMode)

            if len(manga_files) > 0:
                db.insertMangaImages(manga_files)

            # Save tags if enabled
            if config.autoAddTag:
                tags = image.tags
                if tags:
                    for tag_data in tags:
                        tag_id = tag_data.tag
                        if tag_id:
                            db.insertTag(tag_id)
                            db.insertImageToTag(image_id, tag_id)
                            if tag_data.romaji:
                                db.insertTagTranslation(tag_id, 'romaji', tag_data.romaji)
                            if tag_data.translation_data:
                                for locale in tag_data.translation_data:
                                    db.insertTagTranslation(tag_id, locale, tag_data.translation_data[locale])

            # Save member data if enabled
            if image.artist is not None and config.autoAddMember:
                member_id = image.artist.artistId
                member_token = image.artist.artistToken
                member_name = image.artist.artistName
                if member_id and member_token and member_name:
                    db.insertNewMember(int(member_id), member_token=member_token)
                    db.updateMemberName(member_id, member_name, member_token)

            # map back to PIXIVUTIL_OK (because of ugoira file check)
            result = 0

        if image is not None:
            del image
        if parse_medium_page is not None:
            del parse_medium_page
        gc.collect()

        return result
    except Exception as ex:
        if isinstance(ex, KeyboardInterrupt):
            raise
        caller.ERROR_CODE = getattr(ex, 'errorCode', -1)
        exc_type, exc_value, exc_traceback = sys.exc_info()
        traceback.print_exception(exc_type, exc_value, exc_traceback)
        PixivHelper.print_and_log('error', f'Error at process_image(): {image_id}')
        PixivHelper.print_and_log('error', f'Exception: {sys.exc_info()}')

        if parse_medium_page is not None:
            dump_filename = f'Error medium page for image {image_id}.html'
            PixivHelper.dump_html(dump_filename, parse_medium_page)
            PixivHelper.print_and_log('error', f'Dumping html to: {dump_filename}')

        raise


def process_manga_series(caller,
                         config,
                         manga_series_id: int,
                         start_page: int = 1,
                         end_page: int = 0,
                         notifier=None):
    if notifier is None:
        notifier = PixivHelper.dummy_notifier
    try:
        msg = Fore.YELLOW + Style.NORMAL + f'Processing Manga Series Id: {manga_series_id}' + Style.RESET_ALL
        PixivHelper.print_and_log(None, msg)
        notifier(type="MANGA_SERIES", message=msg)

        if start_page != 1:
            PixivHelper.print_and_log('info', 'Start Page: ' + str(start_page))
        if end_page != 0:
            PixivHelper.print_and_log('info', 'End Page: ' + str(end_page))

        flag = True
        current_page = start_page
        while flag:
            manga_series = PixivBrowserFactory.getBrowser().getMangaSeries(manga_series_id, current_page)
            for (image_id, order) in manga_series.pages_with_order:
                result = process_image(caller,
                                       config,
                                       artist=manga_series.artist,
                                       image_id=image_id,
                                       user_dir='',
                                       bookmark=False,
                                       search_tags='',
                                       title_prefix="",
                                       bookmark_count=-1,
                                       image_response_count=-1,
                                       notifier=notifier,
                                       useblacklist=True,
                                       manga_series_order=order,
                                       manga_series_parent=manga_series)
                PixivHelper.wait(result, config)
            current_page += 1
            if manga_series.is_last_page:
                PixivHelper.print_and_log('info', f'Last Page {manga_series.current_page}')
                flag = False
            if current_page > end_page and end_page != 0:
                PixivHelper.print_and_log('info', f'End Page reached {end_page}')
                flag = False
            if manga_series.pages_with_order is None or len(manga_series.pages_with_order) == 0:
                PixivHelper.print_and_log('info', 'No more works.')
                flag = False

    except Exception as ex:
        if isinstance(ex, KeyboardInterrupt):
            raise
        caller.ERROR_CODE = getattr(ex, 'errorCode', -1)
        exc_type, exc_value, exc_traceback = sys.exc_info()
        traceback.print_exception(exc_type, exc_value, exc_traceback)
        PixivHelper.print_and_log('error', f'Error at process_manga_series(): {manga_series_id}')
        PixivHelper.print_and_log('error', f'Exception: {sys.exc_info()}')
        raise


def process_ugoira_local(caller, config):
    directory = config.rootDirectory
    counter = 0
    d = ""
    res = None
    counter = 0
    list_done = list()

    try:
        print('')
        for extension in ["ugoira", "zip"]:  # always ugoira then zip
            for zip in pathlib.Path(directory).rglob(f'*.{extension}'):
                zip_name = os.path.splitext(os.path.basename(zip))[0]
                zip_dir = os.path.dirname(zip)
                image_id = zip_name.partition("_")[0]
                if 'ugoira' in zip_name and image_id not in list_done:
                    counter += 1
                    PixivHelper.print_and_log(None, f"# Ugoira {counter}")
                    PixivHelper.print_and_log("info", "Deleting old animated files ...", newline=False)
                    d = PixivHelper.create_temp_dir(prefix="reencoding")

                    # List and move all files related to the image_id
                    for file in os.listdir(zip_dir):
                        if os.path.isfile(os.path.join(zip_dir, file)) and zip_name in file:
                            file_basename = os.path.basename(file)
                            file_ext = os.path.splitext(file_basename)[1]
                            if ((("gif" in file_ext) and (config.createGif))
                               or (("mkv" in file_ext) and (config.createMkv))
                               or (("png" in file_ext) and (config.createApng))
                               or (("avif" in file_ext) and (config.createAvif))
                               or (("webm" in file_ext) and (config.createWebm))
                               or (("webp" in file_ext) and (config.createWebp))
                               or (("ugoira" in file_ext) and (config.createUgoira))
                               or ("zip" in file_ext)):
                                abs_file_path = os.path.abspath(os.path.join(zip_dir, file))
                                PixivHelper.print_and_log("debug", f"Moving {abs_file_path} to {d}")
                                if ("zip" in file_ext) or ("ugoira" in file_ext):
                                    shutil.copy2(abs_file_path, os.path.join(d, file_basename))
                                else:
                                    shutil.move(abs_file_path, os.path.join(d, file_basename))
                    PixivHelper.print_and_log(None, " done.")

                    # Process artwork locally
                    if "ugoira" in extension and not config.overwrite:
                        try:
                            msg = Fore.YELLOW + Style.NORMAL + f'Processing Image Id: {image_id}' + Style.RESET_ALL
                            PixivHelper.print_and_log(None, msg)
                            PixivDownloadHandler.handle_ugoira(None, str(zip), config, None)
                            res = PixivConstant.PIXIVUTIL_OK
                        except PixivException as ex:
                            PixivHelper.print_and_log('error', f'PixivException for Image ID ({image_id}): {ex}')
                            PixivHelper.print_and_log('error', f'Stack Trace: {sys.exc_info()}')
                            res = PixivConstant.PIXIVUTIL_NOT_OK
                        except Exception as ex:
                            PixivHelper.print_and_log('error', f'Exception for Image ID ({image_id}): {ex}')
                            PixivHelper.print_and_log('error', f'Stack Trace: {sys.exc_info()}')
                            exc_type, exc_value, exc_traceback = sys.exc_info()
                            traceback.print_exception(exc_type, exc_value, exc_traceback)
                            res = PixivConstant.PIXIVUTIL_NOT_OK
                        finally:
                            if res == PixivConstant.PIXIVUTIL_NOT_OK:
                                PixivHelper.print_and_log('warn', f'Failed to process Image ID {image_id} locally: will retry with online infos')
                                PixivHelper.print_and_log('debug', f'Removing corrupted ugoira {zip}')
                                os.remove(zip)

                    # Process artwork with online infos
                    if "zip" in extension or res == PixivConstant.PIXIVUTIL_NOT_OK or ("ugoira" in extension and config.overwrite):
                        res = process_image(caller,
                                            config,
                                            artist=None,
                                            image_id=image_id,
                                            useblacklist=False,
                                            reencoding=True)
                        if res == PixivConstant.PIXIVUTIL_NOT_OK:
                            PixivHelper.print_and_log("warn", f"Cannot process Image Id: {image_id}, restoring old animated files...", newline=False)
                            for file_name in os.listdir(d):
                                PixivHelper.print_and_log("debug", f"Moving back {os.path.join(d, file_name)} to {os.path.join(zip_dir, file_name)}")
                                shutil.move(os.path.join(d, file_name), os.path.join(zip_dir, file_name))  # overwrite corrupted file generated
                            PixivHelper.print_and_log(None, " done.")
                            print('')

                    # Checking result
                    list_file_zipdir = os.listdir(zip_dir)
                    for file_name in os.listdir(d):
                        file_ext = os.path.splitext(file_name)[1]
                        if file_name not in list_file_zipdir and config.backupOldFile:
                            if ((config.createUgoira and not config.deleteUgoira and "ugoira" in file_ext)
                                 or (not config.deleteZipFile and "zip" in file_ext)
                                 or (config.createGif and "gif" in file_ext)
                                 or (config.createApng and "png" in file_ext)
                                 or (config.createAvif and "avif" in file_ext)
                                 or (config.createWebm and "webm" in file_ext)
                                 or (config.createWebp and "webp" in file_ext)):
                                split_name = file_name.rsplit(".", 1)
                                new_name = file_name + "." + str(int(time.time()))
                                if len(split_name) == 2:
                                    new_name = split_name[0] + "." + str(int(time.time())) + "." + split_name[1]
                                PixivHelper.print_and_log('warn', f"Could not found the animated file re-encoded ==> {file_name}, backing up to: {new_name}")
                                PixivHelper.print_and_log('warn', "The new encoded file may have another name or the artist may have change its name.")
                                PixivHelper.print_and_log("debug", f"Rename and move {os.path.join(d, file_name)} to {os.path.join(zip_dir, new_name)}")
                                shutil.move(os.path.join(d, file_name), os.path.join(zip_dir, new_name))
                    print('')

                    # Delete temp path
                    if os.path.exists(d) and d != "":
                        PixivHelper.print_and_log("debug", f"Deleting path {d}")
                        shutil.rmtree(d)
                    list_done.append(image_id)
        if counter == 0:
            PixivHelper.print_and_log('info', "No zip file or ugoira found to re-encode animated files.")

    except Exception as ex:
        if isinstance(ex, KeyboardInterrupt):
            raise
        PixivHelper.print_and_log('error', 'Error at process_ugoira_local(): %s' % str(sys.exc_info()))
        PixivHelper.print_and_log('error', 'failed')
        raise
    finally:
        if os.path.exists(d) and d != "":
            PixivHelper.print_and_log("debug", f"Deleting path {d} in finally")
            shutil.rmtree(d)
