/**
 * OverFlight — Aircraft Icon Classification System
 *
 * Maps aircraft model/type data to icon categories with SVG silhouettes.
 * Each category has a distinct shape and relative size multiplier.
 */

(function () {
    "use strict";

    var ICON_BASE = "/static/img/aircraft/";

    var CATEGORIES = {
        basic: { icon: "unknown.svg", size: 0.6, label: "Aircraft" }
    };

    // Model patterns for classification (checked in order)
    var MODEL_PATTERNS = [
        // Widebody
        { pattern: /\b(747|767|777|787|A330|A340|A350|A380|A3[89]\d|B74|B76|B77|B78|DC-?10|MD-?11|L-?1011|IL-?96)\b/i, category: "widebody" },
        // Narrowbody
        { pattern: /\b(737|757|A318|A319|A320|A321|A220|B73|B75|MD-?[89]0|DC-?9|717|TU-?204|C919|ARJ21)\b/i, category: "narrowbody" },
        // Regional jets
        { pattern: /\b(CRJ|ERJ|E-?1[479]0|E-?175|E-?195|EMB-?145|CL-?[68]00|ARJ|SSJ|328JET)\b/i, category: "regional" },
        // Turboprop
        { pattern: /\b(ATR|DHC-?8|DASH.?8|Q[234]00|SAAB|DO-?328|C208|CARAVAN|PC-?12|BEECH.?1900|KING.?AIR|BE[29]0|B350|BE20|METRO|J31|J32|J41)\b/i, category: "turboprop" },
        // Helicopter
        { pattern: /\b(H[16]\d\d|EC\d{3}|AS\d{3}|S-?[679]\d|AW\d{3}|A109|A139|A169|BELL|R22|R44|R66|MD500|MD520|MD530|MD600|MD900|UH-?60|AH-?64|CH-?47)\b/i, category: "helicopter" },
        // GA twin
        { pattern: /\b(C310|C340|C402|C414|C421|PA-?3[014]|PA-?44|BARON|BE[56]\d|DUKE|AEROSTAR|DA[46]2|P68)\b/i, category: "ga_twin" },
        // GA single
        { pattern: /\b(C150|C152|C162|C172|C177|C180|C182|C185|C206|C210|PA-?28|PA-?32|PA-?46|SR2[02]|SR22|DA[24]0|RV-?\d|MOONEY|BONANZA|BE[3]5|BE36|TB[12]0|TB21|AA-?[15]|GRUMMAN|MAULE|HUSKY)\b/i, category: "ga_single" },
        // Military patterns (operator-based detection is also used)
        { pattern: /\b(F-?1[5678]|F-?22|F-?35|C-?1[37]0|C-?5|C-?17|KC-?\d{2,3}|B-?[12]|B-?52|E-?[236]|P-?[38]|MQ-?\d|RQ-?\d|T-?38|T-?6|V-?22|MV-?22|CV-?22)\b/i, category: "military" }
    ];

    // Military operator keywords
    var MILITARY_OPERATORS = /\b(air force|navy|army|marine|military|usaf|usn|raf|luftwaffe|aeronautica|fuerza|armee)\b/i;

    /**
     * Classify an aircraft and return icon info.
     *
     * @param {Object} data - Enrichment data with fields: model, operator, manufacturer
     * @returns {{ path: string, size: number, category: string, label: string }}
     */
    function getAircraftIcon(data) {
        var info = CATEGORIES.basic;
        return {
            path: ICON_BASE + info.icon,
            size: info.size,
            category: "basic",
            label: info.label
        };
    }

    // Expose globally
    window.OverflightIcons = {
        getAircraftIcon: getAircraftIcon,
        CATEGORIES: CATEGORIES
    };
})();
