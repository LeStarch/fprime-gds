/**
 * json.js:
 *
 * Contains specialized JSON parser to handle non-standard JSON values from the JavaScript perspective. These values
 * are legal in Python and scala, but not in JavaScript. This parser will safely handle these values.
 *
 * @author mstarch
 */


/**
 * Helper to determine if value is a string
 * @param value: value to check.
 * @return {boolean}: true if string, false otherwise
 */
function isString(value) {
    return value instanceof String || typeof value === 'string';
}

/**
 * Helper to determine if value is a function
 * @param value: value to check
 * @return {boolean}: true if function, false otherwise
 */
function isFunction(value) {
    return value instanceof Function || typeof value == "function";
}

/**
 * Conversion function for converting from string to BigInt or Number depending on size
 * @param {*} string_value: value to convert
 * @returns: Number for small values and BigInt for large values
 */
function convertInt(string_value) {
    string_value = string_value.trim();
    let number_value = Number.parseInt(string_value);
    // When the big and normal numbers match, then return the normal number
    if (string_value == number_value.toString()) {
        return number_value;
    }
    return BigInt(string_value);
}

/**
 * Parser to safely handle potential JSON object from Python. Python can produce some non-standard values (infinities,
 * NaNs, etc.) These values then break on the JS Javascript parser. To localize these faults, they are replaced before
 * processing with strings and then formally set during parsing.
 *
 * This is done by looking for tokens in unquoted text and replacing them with string representations.
 * 
 * This parser will handle:
 * - -Infinity
 * - Infinity
 * - NaN
 * - null
 * - BigInt
 */
export class SaferParser {
    /**
     * States representing QUOTED or UNQUOTED text
     * @type {{QUOTED: number, UNQUOTED: number}}
     */
    static STATES = {
        UNQUOTED: 0,
        QUOTED: 1
    };
    /**
     * List of mapping tuples for clean parsing: string match, replacement type, and real (post parse) type
     */
    static MAPPINGS = [
        [/(-Infinity)/, -Infinity],
        [/(Infinity)/, Infinity],
        [/(NaN)/, NaN],
        [/(null)/, null],
        [/( -?\d{10,})/, "bigint", convertInt]
    ];


    // Store the language variants the first time
    static language_parse = JSON.parse;
    static language_stringify = JSON.stringify;

    /**
     * @brief safely process F Prime JSON syntax
     *
     * Parse method that will replace JSON.parse. This method pre-processes the string data incoming (to be transformed
     * into JavaScript objects) for detection of entities not expressible in JavaScript's JSON implementation. This will
     * replace those entities with a JSON flag object.
     *
     * Then the data is processed by the JavaScript built-in JSON parser (now done safely).  The reviver function will
     * safely revive the flag objects into JavaScript representations of those object.
     *
     * Handles:
     * 1. BigInts
     * 2. Inf/-Inf
     * 3. NaN
     * 4. null
     *
     * @param json_string: JSON string data containing potentially bad values
     * @param reviver: reviver function to be combined with our reviver
     * @return {{}}: Javascript Object representation of data safely represented in JavaScript types
     */
    static parse(json_string, reviver) {
        let converted_data = SaferParser.processUnquoted(json_string, SaferParser.replaceFromString);
        // Set up a composite reviver of the one passed in and ours
        let input_reviver = reviver || ((key, value) => value);
        let full_reviver = (key, value) => input_reviver(key, SaferParser.reviver(key, value));
        try {
            let language_parsed = SaferParser.language_parse(converted_data, full_reviver);
            return language_parsed;
        } catch (e) {
            let message = e.toString();
            const matcher = /line (\d+) column (\d+)/

            // Process the match
            let snippet = "";
            let match = message.match(matcher);
            if (match != null) {
                let lines = converted_data.split("\n");
                let line = lines[Number.parseInt(match[1]) - 1]
                snippet = line.substring(Number.parseInt(match[2]) - 6, Number.parseInt(match[2]) + 5);
                message += ". Offending snippet: " + snippet;
                throw new SyntaxError(message);
            }
            throw e;
        }
    }

    /**
     * @brief safely write the F Prime JSON syntax
     *
     * Stringify method that will replace JSON.stringify. This method post-processes the string data outgoing from
     * JavaScript's built-in stringify method to replace flag-objects with the correct F Prime representation in
     * JavaScript.
     *
     * This uses the javascript stringify handler method to pre-convert unsupported types into a flag object. This flag
     * object is post-converted into a normal string after JSON.stringify has done its best.
     *
     * Handles:
     * 1. BigInts
     * 2. Inf/-Inf
     * 3. NaN
     * 4. null
     *
     * @param data: data object to stringify
     * @param replacer: replacer Array or Function
     * @param space: space for passing into JSON.stringify
     * @return {{}}: JSON string using JSON support for big-ints Int/-Inf, NaN and null.
     */
    static stringify(data, replacer, space) {
        let full_replacer = (key, value) => {
            // Handle array case for excluded field
            if (Array.isArray(replacer) && replacer.indexOf(key) === -1) {
                return undefined;
            }
            // Run input replacer first
            else if (isFunction(replacer)) {
                value = replacer(key, value);
            }
            // Then run our safe replacer
            let replaced = SaferParser.replaceFromObject(key, value);
            return replaced;
        };
        // Stringify JSON using built-in JSON parser and the special replacer
        let json_string = SaferParser.language_stringify(data, full_replacer, space);
        // Post-process JSON string to rework JSON into the wider specification
        let post_replace =  SaferParser.postReplacer(json_string);
        return post_replace
    }

    /**
     * Get replacement object from a JavaScript type
     * @param value: value to replace
     */
    static replaceFromObject(_, value) {
        for (let i = 0; i < SaferParser.MAPPINGS.length; i++) {
            let mapper_type = SaferParser.MAPPINGS[i][1];
            let mapper_is_string = isString(mapper_type);
            // Check if the mapping matches the value, if so substitute a replacement object
            if ((!mapper_is_string && value == mapper_type) || (mapper_is_string && typeof value == mapper_type)) {
                return {"fprime{replacement": (value == null) ? "null" : value.toString()};
            }
        }
        return value;
    }

    /**
     * Replace JSON notation for fprime-replacement objects with the wider JSON specification
     *
     * Replace {"fprime-replacement: "some value"} with <some value> restoring the full JSON specification for items not
     * supported by JavaScript.
     *
     * @param json_string: JSON string to rework
     * @return reworked JSON string
     */
    static postReplacer(json_string) {
        return json_string.replace(/\{\s*"fprime\{replacement"\s*:\s*"([^"]+)"\s*\}/sg, "$1");
    }

    /**
     * Replace string occurrences of our gnarly types with a mapping equivalent
     * @param string_value: value to replace
     */
    static replaceFromString(string_value) {
        for (let i = 0; i < SaferParser.MAPPINGS.length; i++) {
            let mapper = SaferParser.MAPPINGS[i];
            string_value = string_value.replace(mapper[0], "{\"fprime{replacement\": \"$1\"}");
        }
        return string_value;
    }

    /**
     * Apply process function to raw json string only for data that is not qu
     * @param json_string
     * @param process_function
     * @return {string}
     */
    static processUnquoted(json_string, process_function) {
        // The initial state of any JSON string is unquoted
        let state = SaferParser.STATES.UNQUOTED;
        let unprocessed = json_string;
        let transformed_data = "";

        while (unprocessed.length > 0) {
            let next_quote = unprocessed.indexOf("\"");
            let section = (next_quote !== -1) ? unprocessed.substring(0, next_quote + 1) : unprocessed.substring(0);
            unprocessed = unprocessed.substring(section.length);
            transformed_data += (state === SaferParser.STATES.QUOTED) ? section : process_function(section);
            state = (state === SaferParser.STATES.QUOTED) ? SaferParser.STATES.UNQUOTED : SaferParser.STATES.QUOTED;
        }
        return transformed_data;
    }

    /**
     * Inverse of convert removing string and replacing back invalid JSON tokens.
     * @param key: JSON key
     * @param value: JSON value search for the converted value.
     * @return {*}: reverted value or value
     */
    static reviver(key, value) {
        // Look for fprime-replacement and quickly abort if not there
        let string_value = value["fprime{replacement"];
        if (typeof string_value === "undefined") {
            return value;
        }
        // Run the mappings looking for a match
        for (let i = 0; i < SaferParser.MAPPINGS.length; i++) {
            let mapper = SaferParser.MAPPINGS[i];
            if (mapper[0].test(string_value)) {
                // Run the conversion function if it exists, otherwise return the mapped constant value
                return (mapper.length >= 3) ? mapper[2](string_value) : mapper[1];
            }
        }
        return value;
    }

     /**
     * @brief force all calls to JSON.parse and JSON.stringify to use the SafeParser
     */
    static register() {
        // Override the singleton
        JSON.parse = SaferParser.parse;
        JSON.stringify = SaferParser.stringify;
    }

    /**
     * @brief remove the JSON.parse safe override
     */
    static deregister() {
        JSON.parse = SaferParser.language_parse;
        JSON.stringify = SaferParser.language_stringify;
    }
}
// Take over all JSON.parse and JSON.stringify calls
SaferParser.register();
