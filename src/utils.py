from psycopg2.extras import RealDictCursor

import traceback
import functools
import psycopg2
import config
import json


def trace_unhandled_exceptions(func):
    @functools.wraps(func)
    def wrapped_func(*args, **kwargs):
        try:
            func(*args, **kwargs)
        except:
            print('Exception in ' + func.__name__ + ' on file ' + args[0])
            traceback.print_exc()
    return wrapped_func


def remove_suffix(input_string, suffix):
    if suffix and input_string.endswith(suffix):
        return input_string[:-len(suffix)]
    return input_string


def remove_prefix(input_string, prefix):
    if prefix and input_string.startswith(prefix):
        return input_string[len(prefix):]
    return input_string


def replace_file_from_post(
    pg_connection: psycopg2.extensions.connection,
    old_file: str,
    new_file: str,
    service=None,
    user_id=None,
    post_id=None,
    min_time=None,
    max_time=None
):
    """
    Updates post that matches `service`, `user_id`, and `post_id`, replacing
    all instances in its data of `old_file` with `new_file`.
    This should be used for complex migrations like `attachment` and `inline` files.
    Otherwise, a one query find/update is probably more ideal.
    """
    updated_rows = 0
    with pg_connection.cursor() as cursor:
        if service and user_id and post_id:
            cursor.execute('SELECT * FROM posts WHERE service = %s AND "user" = %s AND id = %s', (service, user_id, post_id))
        elif user_id and post_id:
            cursor.execute('SELECT * FROM posts WHERE "user" = %s AND id = %s', (user_id, post_id))
        elif min_time and max_time:
            cursor.execute('SELECT * FROM posts WHERE added >= %s AND added < %s', (min_time, max_time))
        else:
            cursor.execute('SELECT * FROM posts')

        first_post = None
        for post_data in cursor:
            original_post_data = post_data

            # Replace.
            post_data['content'] = post_data['content'].replace('https://kemono.party' + old_file, new_file)
            post_data['content'] = post_data['content'].replace(old_file, new_file)
            if post_data['file'].get('path'):
                post_data['file']['path'] = post_data['file']['path'].replace('https://kemono.party' + old_file, new_file)
                post_data['file']['path'] = post_data['file']['path'].replace(old_file, new_file)
            for (i, _) in enumerate(post_data['attachments']):
                if post_data['attachments'][i].get('path'):
                    post_data['attachments'][i]['path'] = post_data['attachments'][i]['path'].replace('https://kemono.party' + old_file, new_file)
                    post_data['attachments'][i]['path'] = post_data['attachments'][i]['path'].replace(old_file, new_file)
            if (original_post_data != post_data or new_file in json.dumps(post_data, default=str)):
                updated_rows += 1
                first_post = first_post or post_data
            else:
                continue

            # Format.
            post_data['embed'] = json.dumps(post_data['embed'])
            post_data['file'] = json.dumps(post_data['file'])
            for i in range(len(post_data['attachments'])):
                post_data['attachments'][i] = json.dumps(post_data['attachments'][i])

            # Update.
            columns = post_data.keys()
            data = ['%s'] * len(post_data.values())
            data[list(columns).index('attachments')] = '%s::jsonb[]'  # attachments
            query = 'UPDATE posts SET {updates} WHERE {conditions}'.format(
                updates=','.join([f'"{column}" = {data[i]}' for (i, column) in enumerate(columns)]),
                conditions='service = %s AND "user" = %s AND id = %s'
            )
            with pg_connection.cursor() as cursor:
                cursor.execute(query, list(post_data.values()) + list((service, user_id, post_id,)))

        return (updated_rows, first_post)


def replace_file_from_discord_message(
    pg_connection: psycopg2.extensions.connection,
    old_file: str,
    new_file: str,
    server_id=None,
    channel_id=None,
    message_id=None,
    min_time=None,
    max_time=None
):
    """
    Updates Discord messages that matches `server_id`, `channel_id`, and
    `message_id`, replacing all instances in its data of `old_file` with `new_file`.
    This should be used for complex migrations like `attachment` and `inline` files.
    Otherwise, a one query find/update is probably more ideal.
    """
    updated_rows = 0
    with pg_connection.cursor() as cursor:
        if server_id and channel_id and message_id:
            cursor.execute('SELECT * FROM discord_posts WHERE server = %s AND channel = %s AND id = %s', (server_id, channel_id, message_id))
        elif server_id and message_id:
            cursor.execute('SELECT * FROM discord_posts WHERE server = %s AND id = %s', (server_id, message_id))
        elif min_time and max_time:
            cursor.execute('SELECT * FROM discord_posts WHERE added >= %s AND added < %s', (min_time, max_time))
        else:
            cursor.execute('SELECT * FROM discord_posts')

        first_message = None
        for post_data in cursor:
            original_message_data = post_data

            # Replace.
            for (i, _) in enumerate(post_data['attachments']):
                if post_data['attachments'][i].get('path'):
                    post_data['attachments'][i]['path'] = post_data['attachments'][i]['path'].replace('https://kemono.party' + old_file, new_file)
                    post_data['attachments'][i]['path'] = post_data['attachments'][i]['path'].replace(old_file, new_file)
            if (original_message_data != post_data or new_file in json.dumps(post_data, default=str)):
                updated_rows += 1
                first_message = first_message or post_data
            else:
                continue

            # Format.
            post_data['author'] = json.dumps(post_data['author'])
            for i in range(len(post_data['attachments'])):
                post_data['attachments'][i] = json.dumps(post_data['attachments'][i])
            for i in range(len(post_data['mentions'])):
                post_data['mentions'][i] = json.dumps(post_data['mentions'][i])
            for i in range(len(post_data['embeds'])):
                post_data['embeds'][i] = json.dumps(post_data['embeds'][i])

            # Update.
            columns = post_data.keys()
            data = ['%s'] * len(post_data.values())
            data[list(columns).index('attachments')] = '%s::jsonb[]'
            data[list(columns).index('mentions')] = '%s::jsonb[]'
            data[list(columns).index('embeds')] = '%s::jsonb[]'
            query = 'UPDATE discord_posts SET {updates} WHERE {conditions}'.format(
                updates=','.join([f'"{column}" = {data[i]}' for (i, column) in enumerate(columns)]),
                conditions='server = %s AND channel = %s AND id = %s'
            )
            with pg_connection.cursor() as cursor:
                cursor.execute(query, list(post_data.values()) + list((server_id, channel_id, message_id,)))

        return (updated_rows, first_message)
