# llm/images.py
# Pictures in a request: a message may carry `images`, a list of
# {'mime': 'image/jpeg', 'data': <base64>}, next to its text `content` (which
# stays a plain string, so everything that reads messages keeps working). Each
# transport turns them into its own wire shape; a model that refuses pictures
# is asked again without them (llm.complete).

from typing import List


def has_images(messages: list) -> bool:
    return any(m.get('images') for m in messages)


# The same messages without their pictures: for a model that can't take them.
def without_images(messages: list) -> list:
    return [{k: v for k, v in m.items() if k != 'images'} for m in messages]


# The message's pictures as data URLs ("data:image/jpeg;base64,…"), the shape
# OpenAI's formats take.
def data_urls(message: dict) -> List[str]:
    return [f"data:{image.get('mime') or 'image/png'};base64,{image['data']}"
            for image in message.get('images') or [] if image.get('data')]
