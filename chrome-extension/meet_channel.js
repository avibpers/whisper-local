// meet_channel.js
// Runs in the Meet page's own world, before Meet's code, because only there
// can a script see the page's WebRTC connections. Meet receives its captions
// over a data channel, whether or not the tab is on screen, while the captions
// on the page stop changing when it is hidden. This script only listens: it
// adds a listener to Meet's channels, changes nothing Meet sends or receives,
// and hands what it decodes to meet.js through a DOM event.
// Meet's messages are undocumented protobuf, with the layouts below taken from
// what they are seen to hold. Anything else is counted and left to meet.js,
// which then keeps reading the page. Design: docs/design/meet-extension.md.

(() => {
  'use strict';

  const EVENT_NAME = 'koli-meet-channel';
  const DEVICE_ID = /^spaces\/.+\/devices\/|^devices\//;
  const MAX_NAME_LENGTH = 200;   // a longer "name" is some other text
  const MAX_WALK_DEPTH = 8;      // how deep a people message is searched for names

  // Protobuf field kinds on the wire.
  const WIRE_VARINT = 0;
  const WIRE_64BIT = 1;
  const WIRE_BYTES = 2;
  const WIRE_32BIT = 5;

  const textDecoder = new TextDecoder();

  // ── Reading protobuf without its schema ────────────────────────────────────

  // A varint at `pos`: [value as BigInt (ids can pass 2^53), the next position].
  function readVarint(bytes, pos) {
    let value = 0n;
    for (let shift = 0n; shift < 70n; shift += 7n) {
      if (pos >= bytes.length) throw new Error('varint runs off the end');
      const byte = bytes[pos++];
      value |= BigInt(byte & 0x7f) << shift;
      if (!(byte & 0x80)) return [value, pos];
    }
    throw new Error('varint too long');
  }

  // A message's fields in order: [{number, wire, value}]. Throws on bytes that
  // aren't a message, which is how a nested message is told from plain text.
  function fields(bytes) {
    const found = [];
    let pos = 0;
    while (pos < bytes.length) {
      const [tag, afterTag] = readVarint(bytes, pos);
      const number = Number(tag >> 3n);
      const wire = Number(tag & 7n);
      if (number < 1) throw new Error('not a field');
      pos = afterTag;
      if (wire === WIRE_VARINT) {
        const [value, next] = readVarint(bytes, pos);
        found.push({ number, wire, value });
        pos = next;
      } else if (wire === WIRE_BYTES) {
        const [length, next] = readVarint(bytes, pos);
        const end = next + Number(length);
        if (end > bytes.length) throw new Error('field runs off the end');
        found.push({ number, wire, value: bytes.subarray(next, end) });
        pos = end;
      } else if (wire === WIRE_64BIT || wire === WIRE_32BIT) {
        pos += wire === WIRE_64BIT ? 8 : 4;
        if (pos > bytes.length) throw new Error('field runs off the end');
      } else {
        throw new Error('not a message');
      }
    }
    return found;
  }

  function fieldOf(list, number, wire) {
    return list.find((f) => f.number === number && f.wire === wire);
  }

  function textOf(list, number) {
    const field = fieldOf(list, number, WIRE_BYTES);
    return field ? textDecoder.decode(field.value) : '';
  }

  function numberTextOf(list, number) {
    const field = fieldOf(list, number, WIRE_VARINT);
    return field ? field.value.toString() : '';
  }

  function messageOf(list, number) {
    const field = fieldOf(list, number, WIRE_BYTES);
    return field ? fields(field.value) : [];
  }

  // ── Caption messages ───────────────────────────────────────────────────────

  // Channel "captions": {1: {1 device, 2 id, 3 version, 4 final, 6 text}}.
  function captionV1(bytes) {
    const caption = messageOf(fields(bytes), 1);
    return { device: textOf(caption, 1), id: numberTextOf(caption, 2), version: numberTextOf(caption, 3),
             final: numberTextOf(caption, 4) === '1', text: textOf(caption, 6) };
  }

  // Channel "captions_v2": {1: {1 id, 2 version, 3: {2 final, 3 text, 6 device}}}.
  function captionV2(bytes) {
    const event = messageOf(fields(bytes), 1);
    const caption = messageOf(event, 3);
    return { device: textOf(caption, 6), id: numberTextOf(event, 1), version: numberTextOf(event, 2),
             final: numberTextOf(caption, 2) === '1', text: textOf(caption, 3) };
  }

  // One caption message, or null when it holds neither layout. The channel's
  // label picks the layout tried first: a new label may carry either.
  function parseCaption(label, bytes) {
    const layouts = label.endsWith('v2') ? [captionV2, captionV1] : [captionV1, captionV2];
    for (const layout of layouts) {
      try {
        const caption = layout(bytes);
        if (DEVICE_ID.test(caption.device) && caption.id) return caption;
      } catch (e) {
        // Not this layout.
      }
    }
    return null;
  }

  // ── Who is who ─────────────────────────────────────────────────────────────

  // Every person in a "collections" message, however deep:
  // {1 device, 2 full name, 7 present only on you, 29 display name}. The list
  // sits under wrappers Meet changes, so it is found by its shape.
  function findUsers(bytes, depth = 0, out = []) {
    let list;
    try {
      list = fields(bytes);
    } catch (e) {
      return out;
    }
    const device = textOf(list, 1);
    if (DEVICE_ID.test(device)) {
      const name = textOf(list, 29) || textOf(list, 2);
      if (name && name.length <= MAX_NAME_LENGTH) {
        out.push({ device, name, self: !!fieldOf(list, 7, WIRE_BYTES) });
      }
    }
    if (depth < MAX_WALK_DEPTH) {
      for (const field of list) {
        if (field.wire === WIRE_BYTES) findUsers(field.value, depth + 1, out);
      }
    }
    return out;
  }

  // ── Listening to the channels ──────────────────────────────────────────────

  function post(message) {
    document.dispatchEvent(new CustomEvent(EVENT_NAME, { detail: JSON.stringify(message) }));
  }

  // Message data as bytes, unpacked when Meet compressed it.
  async function bytesOf(data) {
    let bytes;
    if (data instanceof ArrayBuffer) bytes = new Uint8Array(data);
    else if (ArrayBuffer.isView(data)) bytes = new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
    else if (data instanceof Blob) bytes = new Uint8Array(await data.arrayBuffer());
    else return null;
    const format = bytes[0] === 0x1f && bytes[1] === 0x8b ? 'gzip' : bytes[0] === 0x78 ? 'deflate' : null;
    if (!format) return bytes;
    try {
      const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream(format));
      return new Uint8Array(await new Response(stream).arrayBuffer());
    } catch (e) {
      return bytes;   // it only looked compressed
    }
  }

  async function onMessage(label, data) {
    const bytes = await bytesOf(data);
    if (!bytes) return;
    if (label === 'collections') {
      const users = findUsers(bytes);
      if (users.length) post({ kind: 'users', users });
      return;
    }
    const caption = parseCaption(label, bytes);
    post(caption ? { kind: 'caption', caption } : { kind: 'undecoded', label });
  }

  const watched = new WeakSet();

  function watchChannel(channel) {
    if (watched.has(channel)) return;
    watched.add(channel);
    const label = String(channel.label || '');
    post({ kind: 'channel', label });
    if (label !== 'collections' && !label.startsWith('captions')) return;
    channel.addEventListener('message', (event) => { onMessage(label, event.data).catch(() => {}); });
  }

  // The people list comes on a channel the far end opens.
  function watchConnection(connection) {
    if (watched.has(connection)) return;
    watched.add(connection);
    connection.addEventListener('datachannel', (event) => { watchChannel(event.channel); });
  }

  // The one thing patched: createDataChannel still does what it did, and we
  // also get to listen. A failure here must never reach Meet.
  function install() {
    const proto = RTCPeerConnection.prototype;
    const create = proto.createDataChannel;
    proto.createDataChannel = function createDataChannel(...args) {
      const channel = create.apply(this, args);
      try {
        watchConnection(this);
        watchChannel(channel);
      } catch (e) {
        // Meet's call matters more than our transcript.
      }
      return channel;
    };
  }

  if (typeof RTCPeerConnection === 'function') install();
  if (typeof module === 'object' && module.exports) module.exports = { parseCaption, findUsers };
})();
