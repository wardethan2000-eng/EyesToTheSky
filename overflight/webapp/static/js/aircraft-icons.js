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
        widebody: { icon: "widebody.svg", size: 1.0, label: "Widebody Jet" },
        narrowbody: { icon: "narrowbody.svg", size: 0.92, label: "Narrowbody Jet" },
        regional: { icon: "regional.svg", size: 0.82, label: "Regional Jet" },
        turboprop: { icon: "turboprop.svg", size: 0.78, label: "Turboprop" },
        helicopter: { icon: "helicopter.svg", size: 0.74, label: "Helicopter" },
        ga_twin: { icon: "ga_twin.svg", size: 0.72, label: "Twin-Engine GA" },
        ga_single: { icon: "ga_single.svg", size: 0.68, label: "General Aviation" },
        military: { icon: "military.svg", size: 0.88, label: "Military" },
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
        { pattern: /\b(C-?(?:310|340|402|414|421)[A-Z]?|PA-?3[014][A-Z0-9-]*|PA-?44[A-Z0-9-]*|BARON|BE[56]\d[A-Z]?|DUKE|AEROSTAR|DA[46]2[A-Z0-9-]*|P68[A-Z0-9-]*)\b/i, category: "ga_twin" },
        // GA single
        { pattern: /\b(C-?(?:150|152|162|172|177|180|182|185|206|210)[A-Z]?|PA-?28[A-Z0-9-]*|PA-?32[A-Z0-9-]*|PA-?46[A-Z0-9-]*|SR2[02][A-Z]?|DA[24]0[A-Z0-9-]*|RV-?\d+[A-Z]?|MOONEY|BONANZA|BE(?:35|36)[A-Z]?|TB(?:10|12|20|21)[A-Z]?|AA-?[15][A-Z]?|GRUMMAN|MAULE|HUSKY|SKYHAWK|SKYLANE|STATIONAIR)\b/i, category: "ga_single" },
        // Military patterns (operator-based detection is also used)
        { pattern: /\b(F-?1[5678]|F-?22|F-?35|C-?1[37]0|C-?5|C-?17|KC-?\d{2,3}|B-?[12]|B-?52|E-?[236]|P-?[38]|MQ-?\d|RQ-?\d|T-?38|T-?6|V-?22|MV-?22|CV-?22)\b/i, category: "military" }
    ];

    var COMPACT_MODEL_PATTERNS = [
        { pattern: /^(?:747|767|777|787|A330|A340|A350|A380|A3[89]\d|B74|B76|B77|B78|DC10|MD11|L1011|IL96)/i, category: "widebody" },
        { pattern: /^(?:737|757|A318|A319|A320|A321|A220|B73|B75|MD(?:80|90)|DC9|717|TU204|C919|ARJ21)/i, category: "narrowbody" },
        { pattern: /^(?:CRJ|ERJ|E(?:170|175|190|195)|EMB145|CL(?:600|650|850)|ARJ|SSJ|328JET)/i, category: "regional" },
        { pattern: /^(?:ATR|DHC8|DASH8|Q[234]00|SAAB|DO328|C208|CARAVAN|PC12|BEECH1900|KINGAIR|BE(?:20|90)|B350|METRO|J31|J32|J41)/i, category: "turboprop" },
        { pattern: /^(?:H[16]\d\d|EC\d{3}|AS\d{3}|S[679]\d|AW\d{3}|A109|A139|A169|BELL|R22|R44|R66|MD(?:500|520|530|600|900)|UH60|AH64|CH47)/i, category: "helicopter" },
        { pattern: /^(?:C(?:310|340|402|414|421)|PA(?:30|31|34|44)|BARON|BE[56]\d|DUKE|AEROSTAR|DA[46]2|P68|SENECA|TWINCOMANCHE)/i, category: "ga_twin" },
        { pattern: /^(?:C(?:150|152|162|172|177|180|182|185|206|210)|PA(?:28|32|46)|SR2[02]|DA[24]0|RV\d+|MOONEY|BONANZA|BE(?:35|36)|TB(?:10|12|20|21)|AA[15]|GRUMMAN|MAULE|HUSKY|SKYHAWK|SKYLANE|STATIONAIR|CHEROKEE|ARCHER|WARRIOR|ARROW|DAKOTA|MALIBU|MIRAGE|MATRIX|COMANCHE)/i, category: "ga_single" },
        { pattern: /^(?:F(?:15|16|17|18|22|35)|C(?:130|135|17|5)|KC\d{2,3}|B(?:1|2|52)|E(?:2|3|6)|P(?:3|8)|MQ\d+|RQ\d+|T(?:38|6)|(?:V|MV|CV)22)/i, category: "military" }
    ];

    var CESSNA_SINGLE_MODELS = /^(?:150|152|162|172|177|180|182|185|206|210)[A-Z]?$/i;
    var CESSNA_TWIN_MODELS = /^(?:310|340|402|414|421)[A-Z]?$/i;

    // Military operator keywords
    var MILITARY_OPERATORS = /\b(air force|navy|army|marine|military|usaf|usn|raf|luftwaffe|aeronautica|fuerza|armee)\b/i;

    function normalizeText(value) {
        return value == null ? "" : String(value).trim();
    }

    function normalizeCompact(value) {
        return normalizeText(value).toUpperCase().replace(/[^A-Z0-9]/g, "");
    }

    function matchCategoryFromPatterns(value, patterns) {
        var index;

        for (index = 0; index < patterns.length; index += 1) {
            if (patterns[index].pattern.test(value)) {
                return patterns[index].category;
            }
        }

        return null;
    }

    function getCategoryInfo(category) {
        return CATEGORIES[category] || CATEGORIES.basic;
    }

    function detectAircraftCategory(data) {
        var model = normalizeText(data && data.model);
        var manufacturer = normalizeText(data && data.manufacturer);
        var operator = normalizeText(data && data.operator);
        var hint = normalizeText(data && data.category);
        var haystack = [model, manufacturer].filter(Boolean).join(" ");
        var compactModel = normalizeCompact(model);
        var compactManufacturer = normalizeCompact(manufacturer);
        var category;

        if (hint && CATEGORIES[hint]) {
            return hint;
        }

        if (operator && MILITARY_OPERATORS.test(operator)) {
            return "military";
        }

        if (compactManufacturer.indexOf("CESSNA") !== -1) {
            if (CESSNA_TWIN_MODELS.test(compactModel)) {
                return "ga_twin";
            }

            if (CESSNA_SINGLE_MODELS.test(compactModel)) {
                return "ga_single";
            }
        }

        category = matchCategoryFromPatterns(haystack, MODEL_PATTERNS);
        if (category) {
            return category;
        }

        category = matchCategoryFromPatterns(compactModel, COMPACT_MODEL_PATTERNS);
        if (category) {
            return category;
        }

        return "basic";
    }

    /**
     * Classify an aircraft and return icon info.
     *
     * @param {Object} data - Enrichment data with fields: model, operator, manufacturer
     * @returns {{ path: string, size: number, category: string, label: string }}
     */
    function getAircraftIcon(data) {
        var category = detectAircraftCategory(data || {});
        var info = getCategoryInfo(category);

        return {
            path: ICON_BASE + info.icon,
            size: info.size,
            category: category,
            label: info.label
        };
    }

    // Expose globally
    window.OverflightIcons = {
        getAircraftIcon: getAircraftIcon,
        CATEGORIES: CATEGORIES
    };
})();
