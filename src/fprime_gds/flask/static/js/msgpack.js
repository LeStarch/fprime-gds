/**
 * msgpack.js - Minimal MessagePack decoder for WebSocket streaming.
 *
 * Implements the subset of the MessagePack specification needed to decode
 * GDS WebSocket frames.  Binary (bin) payloads are returned as Uint8Array;
 * all other types map to their natural JavaScript equivalents.
 *
 * No external dependencies.  Wire-compatible with the standard msgpack
 * format so the server can use any conforming encoder.
 */

const _decoder = new TextDecoder("utf-8");

/**
 * Decode a MessagePack-encoded ArrayBuffer into a JavaScript value.
 * @param {ArrayBuffer} buffer
 * @returns {*} decoded value
 */
export function decode(buffer) {
    const view = new DataView(buffer);
    const uint8 = new Uint8Array(buffer);
    const state = {offset: 0};
    return _unpack(view, uint8, state);
}

function _unpack(view, uint8, state) {
    const byte = view.getUint8(state.offset++);

    // positive fixint 0x00..0x7f
    if (byte <= 0x7F) return byte;
    // fixmap 0x80..0x8f
    if ((byte & 0xF0) === 0x80) return _readMap(view, uint8, state, byte & 0x0F);
    // fixarray 0x90..0x9f
    if ((byte & 0xF0) === 0x90) return _readArray(view, uint8, state, byte & 0x0F);
    // fixstr 0xa0..0xbf
    if ((byte & 0xE0) === 0xA0) return _readStr(uint8, state, byte & 0x1F);
    // negative fixint 0xe0..0xff
    if (byte >= 0xE0) return byte - 256;

    switch (byte) {
        // nil, bool
        case 0xC0: return null;
        case 0xC2: return false;
        case 0xC3: return true;

        // bin 8 / 16 / 32
        case 0xC4: return _readBin(uint8, state, view.getUint8(state.offset++));
        case 0xC5: { let n = view.getUint16(state.offset); state.offset += 2; return _readBin(uint8, state, n); }
        case 0xC6: { let n = view.getUint32(state.offset); state.offset += 4; return _readBin(uint8, state, n); }

        // float 32 / 64
        case 0xCA: { let v = view.getFloat32(state.offset); state.offset += 4; return v; }
        case 0xCB: { let v = view.getFloat64(state.offset); state.offset += 8; return v; }

        // uint 8 / 16 / 32 / 64
        case 0xCC: return view.getUint8(state.offset++);
        case 0xCD: { let v = view.getUint16(state.offset); state.offset += 2; return v; }
        case 0xCE: { let v = view.getUint32(state.offset); state.offset += 4; return v; }
        case 0xCF: {
            let hi = view.getUint32(state.offset);
            let lo = view.getUint32(state.offset + 4);
            state.offset += 8;
            return hi * 0x100000000 + lo;
        }

        // int 8 / 16 / 32 / 64
        case 0xD0: { let v = view.getInt8(state.offset); state.offset += 1; return v; }
        case 0xD1: { let v = view.getInt16(state.offset); state.offset += 2; return v; }
        case 0xD2: { let v = view.getInt32(state.offset); state.offset += 4; return v; }
        case 0xD3: {
            let hi = view.getInt32(state.offset);
            let lo = view.getUint32(state.offset + 4);
            state.offset += 8;
            return hi * 0x100000000 + lo;
        }

        // str 8 / 16 / 32
        case 0xD9: return _readStr(uint8, state, view.getUint8(state.offset++));
        case 0xDA: { let n = view.getUint16(state.offset); state.offset += 2; return _readStr(uint8, state, n); }
        case 0xDB: { let n = view.getUint32(state.offset); state.offset += 4; return _readStr(uint8, state, n); }

        // array 16 / 32
        case 0xDC: { let n = view.getUint16(state.offset); state.offset += 2; return _readArray(view, uint8, state, n); }
        case 0xDD: { let n = view.getUint32(state.offset); state.offset += 4; return _readArray(view, uint8, state, n); }

        // map 16 / 32
        case 0xDE: { let n = view.getUint16(state.offset); state.offset += 2; return _readMap(view, uint8, state, n); }
        case 0xDF: { let n = view.getUint32(state.offset); state.offset += 4; return _readMap(view, uint8, state, n); }

        default:
            throw new Error("Unknown msgpack type: 0x" + byte.toString(16));
    }
}

function _readStr(uint8, state, length) {
    let bytes = uint8.subarray(state.offset, state.offset + length);
    state.offset += length;
    return _decoder.decode(bytes);
}

function _readBin(uint8, state, length) {
    let copy = uint8.slice(state.offset, state.offset + length);
    state.offset += length;
    return copy;
}

function _readArray(view, uint8, state, count) {
    let arr = new Array(count);
    for (let i = 0; i < count; i++) {
        arr[i] = _unpack(view, uint8, state);
    }
    return arr;
}

function _readMap(view, uint8, state, count) {
    let obj = {};
    for (let i = 0; i < count; i++) {
        let key = _unpack(view, uint8, state);
        obj[key] = _unpack(view, uint8, state);
    }
    return obj;
}
