import hashlib
import os
from ..utils import trace_unhandled_exceptions, remove_suffix, remove_prefix, replace_file_from_post, replace_file_from_discord_message
import config
import psycopg2
import pathlib
import datetime
import magic
import re
import mimetypes
import requests
from psycopg2.extras import RealDictCursor
from retry import retry


@trace_unhandled_exceptions
@retry(tries=5)
def migrate_attachment(
    path,
    migration_id,
    _service=None,
    _user_id=None,
    _post_id=None,
    _server_id=None,
    _channel_id=None,
    _message_id=None
):
    if not os.path.exists(path):
        return

    # check if the file is special (symlink, hardlink, empty) and return if so
    if os.path.islink(path) or os.path.getsize(path) == 0 or os.path.ismount(path):
        return

    if config.ignore_temp_files and path.endswith('.temp'):
        return
    
    file_ext = os.path.splitext(path)[1]
    web_path = path.replace(remove_suffix(config.data_dir, '/'), '')
    service = _service or None
    post_id = _post_id or None
    user_id = _user_id or None
    server_id = _server_id or None
    channel_id = _channel_id or None
    message_id = _message_id or None
    with open(path, 'rb') as f:
        # get hash and filename
        file_hash_raw = hashlib.sha256()
        for chunk in iter(lambda: f.read(8192), b''):
            file_hash_raw.update(chunk)
        file_hash = file_hash_raw.hexdigest()
        new_filename = os.path.join('/', file_hash[0:2], file_hash[2:4], file_hash)
        
        mime = magic.from_file(path, mime=True)
        if (config.fix_extensions):
            file_ext = mimetypes.guess_extension(mime or 'application/octet-stream', strict=False)
            new_filename = new_filename + (re.sub('^.jpe$', '.jpg', file_ext or '.bin') if config.fix_jpe else file_ext or '.bin')
        else:
            new_filename = new_filename + (re.sub('^.jpe$', '.jpg', file_ext or '.bin') if config.fix_jpe else file_ext or '.bin')
        
        fname = pathlib.Path(path)
        mtime = datetime.datetime.fromtimestamp(fname.stat().st_mtime)
        ctime = datetime.datetime.fromtimestamp(fname.stat().st_ctime)

        conn = psycopg2.connect(
            host=config.database_host,
            dbname=config.database_dbname,
            user=config.database_user,
            password=config.database_password,
            port=5432,
            cursor_factory=RealDictCursor
        )

        # log to file tracking table
        if (not config.dry_run):
            cursor = conn.cursor()
            cursor.execute("INSERT INTO files (hash, mtime, ctime, mime, ext) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (hash) DO UPDATE SET hash = EXCLUDED.hash RETURNING id", (file_hash, mtime, ctime, mime, file_ext))
            file_id = cursor.fetchone()['id']
        
        updated_rows = 0
        step = 99
        if (service and user_id and post_id):
            (_updated_rows, _) = replace_file_from_post(
                conn,
                service=service,
                user_id=user_id,
                post_id=post_id,
                old_file=web_path,
                new_file=new_filename
            )
            updated_rows = _updated_rows

        if (server_id and channel_id and message_id):
            (_updated_rows, _) = replace_file_from_discord_message(
                conn,
                server_id=server_id,
                channel_id=channel_id,
                message_id=message_id,
                old_file=web_path,
                new_file=new_filename
            )
            updated_rows = _updated_rows

        # update "attachment" path references in db, using different strategies to speed the operation up
        # strat 1: attempt to derive the user and post id from the original path
        if (len(web_path.split('/')) >= 4 and updated_rows == 0):
            step = 1
            guessed_post_id = web_path.split('/')[-2]
            guessed_user_id = web_path.split('/')[-3]
            (_updated_rows, post) = replace_file_from_post(
                conn,
                user_id=guessed_user_id,
                post_id=guessed_post_id,
                old_file=web_path,
                new_file=new_filename
            )
            updated_rows = _updated_rows
            if (post):
                service = post['service']
                user_id = post['user']
                post_id = post['id']
        
        # Discord
        if (updated_rows == 0 and len(web_path.split('/')) >= 4):
            step = 2
            (_updated_rows, message) = replace_file_from_discord_message(
                conn,
                message_id=web_path.split('/')[-2],
                server_id=web_path.split('/')[-3],
                old_file=web_path,
                new_file=new_filename
            )
            updated_rows = _updated_rows
            if (message):
                server_id = message['server']
                channel_id = message['channel']
                message_id = message['id']
        
        # strat 2: attempt to scope out posts archived up to 1 hour after the file was modified (kemono data should almost never change)
        if updated_rows == 0:
            step = 3
            (_updated_rows, post) = replace_file_from_post(
                conn,
                min_time=mtime,
                max_time=mtime + datetime.timedelta(hours=1),
                old_file=web_path,
                new_file=new_filename
            )
            updated_rows = _updated_rows
            if (post):
                service = post['service']
                user_id = post['user']
                post_id = post['id']

        # optimizations didn't work, scan the entire table
        if updated_rows == 0:
            step = 4
            (_updated_rows, post) = replace_file_from_post(
                conn,
                old_file=web_path,
                new_file=new_filename
            )
            updated_rows = _updated_rows
            if (post):
                service = post['service']
                user_id = post['user']
                post_id = post['id']
        
        if (updated_rows == 0):
            step = 5
            (_updated_rows, message) = replace_file_from_discord_message(
                conn,
                old_file=web_path,
                new_file=new_filename
            )
            updated_rows = _updated_rows
            if (message):
                server_id = message['server']
                channel_id = message['channel']
                message_id = message['id']
        
        # log file post relationship (not discord)
        if (not config.dry_run and updated_rows > 0 and service and user_id and post_id):
            cursor = conn.cursor()
            cursor.execute("INSERT INTO file_post_relationships (file_id, filename, service, \"user\", post, inline) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING", (file_id, os.path.basename(path), service, user_id, post_id, False))
        # log file post relationship (discord)
        elif (not config.dry_run and updated_rows > 0 and server_id and channel_id and message_id):
            cursor = conn.cursor()
            cursor.execute("INSERT INTO file_discord_message_relationships (file_id, filename, server, channel, id) VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING", (file_id, os.path.basename(path), server_id, channel_id, message_id))
        
        # log to sdkdd_migration_{migration_id}
        if (not config.dry_run):
            cursor = conn.cursor()
            cursor.execute(f"INSERT INTO sdkdd_migration_{migration_id} (old_location, new_location, ctime, mtime) VALUES (%s, %s, %s, %s)", (web_path, new_filename, mtime, ctime))
            cursor.close()

        # commit db
        if (config.dry_run):
            conn.rollback()
        else:
            conn.commit()
        
        if (not config.dry_run):
            new_filename_without_prefix = remove_prefix(new_filename, '/')
            web_path_without_prefix = remove_prefix(web_path, '/')
            # move to hashy location, do nothing if something is already there
            # move thumbnail to hashy location
            thumb_dir = config.thumb_dir or os.path.join(config.data_dir, 'thumbnail')
            if os.path.isfile(os.path.join(thumb_dir, web_path_without_prefix)) and not os.path.isfile(os.path.join(thumb_dir, new_filename_without_prefix)):
                os.makedirs(os.path.join(thumb_dir, file_hash[0:2], file_hash[2:4]), exist_ok=True)
                os.rename(os.path.join(thumb_dir, web_path_without_prefix), os.path.join(thumb_dir, new_filename_without_prefix))
            
            if os.path.isfile(path) and not os.path.isfile(os.path.join(config.data_dir, new_filename_without_prefix)):
                os.makedirs(os.path.join(config.data_dir, file_hash[0:2], file_hash[2:4]), exist_ok=True)
                os.rename(path, os.path.join(config.data_dir, new_filename_without_prefix))

        if (not config.dry_run and config.ban_url and service and user_id):
            requests.request('BAN', f"{config.ban_url}/{service}/user/" + user_id)
        
        conn.close()

        # done!
        if (service and user_id and post_id):
            print(f'{web_path}\t{new_filename}\t({updated_rows} database entries updated; {service}/{user_id}/{post_id}, found at step {step})')
        elif (server_id and channel_id and message_id):
            print(f'{web_path}\t{new_filename}\t({updated_rows} database entries updated; discord/{server_id}/{channel_id}/{message_id}, found at step {step})')
        else:
            print(f'{web_path}\t{new_filename}\t({updated_rows} database entries updated; no post/messages found)')