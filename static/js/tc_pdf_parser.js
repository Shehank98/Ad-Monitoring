/* ────────────────────────────────────────────────────────────────────────────
   Shared in-browser TC PDF parser.

   This is the SINGLE source of truth for client-side TC PDF parsing. It is used
   by BOTH:
     • templates/tc/pdf_convert.html   (the standalone "TC Converter" page)
     • templates/tc/upload.html        (the main "Upload TC" preview flow)

   Requires pdf.js (window.pdfjsLib) to be loaded on the page.

   Exposes a global:  window.TCPDFParser = { CHANNEL_MAP, normalizeChannel, parsePDF }

   parsePDF(file, selectedChannel) resolves to an array of rows keyed with the
   standard Nova columns:
       { "Date": "D/M/YYYY", "Programme", "Aired Time", "TC Theme", "Duration" }
   ──────────────────────────────────────────────────────────────────────────── */
(function (global) {

  /* ── Channel normalization ────────────────────────────────────────────────
     The Nova system stores channel names with a "Tv - " prefix and varied
     spelling/spacing/casing ("Tv - Hiru TV", "TV - Hiru TV", "TV-Hiru TV"…).
     Parsing rules below key off canonical names, so the selected channel is
     normalized once. The original (Nova) name is still sent to the server so
     it matches the stored Schedule channel.

     Matching is deliberately forgiving: exact map hit first (any case), then
     keyword matching on the name with the "Tv -" prefix stripped. Radio
     channels are never normalized to a TV channel (they share keywords like
     "Hiru"/"Sirasa"), so they keep the generic parsing rules. */
  const CHANNEL_MAP = {
    "Tv - Derana":               "TV Derana",
    "Tv - Hiru TV":              "Hiru TV",
    "Tv - Sirasa TV":            "Sirasa TV",
    "Tv - Shakthi TV":           "Shakthi TV",
    "Tv - Siyatha Tv":           "Siyatha TV",
    "TV -Supreme TV":            "Supreme TV",
    "TV - Supreme TV":           "Supreme TV",
    "Tv - ITN":                  "ITN",
    "Tv - Vasantham TV":         "Vasantham TV",
    "Tv - Swarnavahini":         "Swarnawahini TV",
    "Tv - Channel Eye - Nethra": "Channel Eye",
    "Tv - Rupavahini":           "Rupavahini",
    "Tv - Varnam TV":            "Varnam TV",
  };

  /* Keyword → canonical TV channel. First hit wins. */
  const CHANNEL_KEYWORDS = [
    ["sirasa",      "Sirasa TV"],
    ["hiru",        "Hiru TV"],
    ["shakthi",     "Shakthi TV"],
    ["derana",      "TV Derana"],
    ["siyatha",     "Siyatha TV"],
    ["supreme",     "Supreme TV"],
    ["vasantham",   "Vasantham TV"],
    ["swarna",      "Swarnawahini TV"],   // covers Swarnavahini / Swarnawahini
    ["star tamil",  "Star Tamil"],
    ["varnam",      "Varnam TV"],
    ["rupavahini",  "Rupavahini"],
    ["channel eye", "Channel Eye"],
    ["nethra",      "Channel Eye"],
  ];

  function normalizeChannel(name) {
    if (!name) return name;
    const collapsed = String(name).replace(/\s+/g, ' ').trim();
    if (CHANNEL_MAP[collapsed]) return CHANNEL_MAP[collapsed];

    const lower = collapsed.toLowerCase();

    // Exact map hit, ignoring case ("TV - Hiru TV" vs map key "Tv - Hiru TV")
    for (const key in CHANNEL_MAP) {
      if (key.toLowerCase() === lower) return CHANNEL_MAP[key];
    }

    // Radio channels keep their own name → generic parsing rules
    if (/\bradio\b|\bfm\b/.test(lower)) return collapsed;

    for (const [kw, canonical] of CHANNEL_KEYWORDS) {
      if (lower.includes(kw)) return canonical;
    }
    if (/\bitn\b/.test(lower)) return "ITN";

    return collapsed;
  }

  /* ── In-browser PDF heuristic parser (channel-specific) ───────────────────
     Returns rows keyed with the standard Nova columns:
       Date, Programme, Aired Time, TC Theme, Duration                        */
  async function parsePDF(file, selectedChannel) {
    const normalizedChannel = normalizeChannel(selectedChannel);
    console.log("Selected Channel:", selectedChannel);
    console.log("Normalized Channel:", normalizedChannel);

    const arrayBuffer = await file.arrayBuffer();
    const pdf = await global.pdfjsLib.getDocument({ data: arrayBuffer }).promise;
    const extractedRows = [];

    for (let i = 1; i <= pdf.numPages; i++) {
      const page = await pdf.getPage(i);
      const textContent = await page.getTextContent();
      const items = textContent.items;
      items.sort((a, b) => {
        const yDiff = b.transform[5] - a.transform[5];
        if (Math.abs(yDiff) > 5) return yDiff;
        return a.transform[4] - b.transform[4];
      });
      let currentLine = [], currentY = null, lastEndX = null;
      items.forEach(item => {
        const y = item.transform[5], x = item.transform[4];
        let str = item.str;
        if (normalizedChannel === 'Supreme TV') str = str.replace(/\$/g, '');
        if (!str.trim()) return;
        const charWidth = item.width / Math.max(1, str.length);
        const parts = str.split(/(\s{2,})/);
        let cursorX = x;
        parts.forEach(part => {
          if (part.trim() === '') { cursorX += part.length * charWidth; return; }
          let chunkX = cursorX, text = part.trim(), chunkWidth = text.length * charWidth;
          cursorX += part.length * charWidth;
          if (currentY === null || Math.abs(currentY - y) > 5) {
            if (currentLine.length > 0) extractedRows.push(currentLine);
            currentLine = [{ text, x: chunkX, endX: chunkX + chunkWidth }];
            currentY = y; lastEndX = chunkX + chunkWidth;
          } else {
            const gap = chunkX - lastEndX;
            if (gap > 4) {
              currentLine.push({ text, x: chunkX, endX: chunkX + chunkWidth });
            } else {
              let addSpace = (gap > 1.5 && !currentLine[currentLine.length-1].text.endsWith(' ')) ? ' ' : '';
              currentLine[currentLine.length-1].text += addSpace + text;
              currentLine[currentLine.length-1].endX = chunkX + chunkWidth;
            }
            lastEndX = currentLine[currentLine.length-1].endX;
          }
        });
      });
      if (currentLine.length > 0) extractedRows.push(currentLine);
    }

    const extractedRecords = [];
    extractedRows.forEach(line => {
      const textLine = line.map(i => i.text).join(' ');
      if (/\b(VAT No|Invoice No|Invoice Details|Date\s*:|From\s*:|To\s*:|Contractor Number|Contract No|Schedule No|Client\s*:|Agent\s*:|Ref No|Client Code|Agent Code|Total Value|Certified By|Checked By|Prepared By|POWER HOUSE|UNLESS OBJECTIONS|Page \d+(?: of \d+)?|Prog\. Date|Caption\/Copy Title|Duration \(sec\)|Channel\s*:|Sr\.No\.|MTV Channel|Braybrooke|TELECAST CERTIFICATE|Advertiser\s*:|Address\s*:|Telecast Certificate dt|Campaign Period|Account Executive|Marketing Executive|Co\/Agency|Release Order Ref|Traffic Order No|Reg No\.|Brand\s*:|TOTAL SPOTS|AUTHORISED SIGNATORY|System Generated|Digitally Signed|Day Time|Duration Lan Mat|Cut No|Product Details|Posi|Position|Net Rate|Remarks|Bill Number|Booking Number|Deal Number|Agency Reference No|Total Spot|We certify the commercials|Net Rate 1|Agency Name|Printed Date|Printed Time|Printed By|Client Name|Agent Name|Client \/ Contract Debits|Asia Broadcasting|TELECAST T\/BELT|PRODUCT|DURA\.?|Gross Value|Net Value|Agency Comm|Sub Total|Total Amount|Taxes|SSCL|E\s*&\s*O\.?\s*E|Errors\s*&\s*Omissions|For MTV|Telecast Summary|Campaign Summary|Total Cost|Payment Terms|Certificate of Transmission|Voice of Asia|This is a computer generated|Transmission certificate|no signature is required|J Lanka Group Of Companies|Authorized Signature|TC_TIME|Supreme TV|Actual time|Time Belt|Time\s*Programme\s*Date|Programme\s*Date)\b/i.test(textLine) || /Rs\s*:/i.test(textLine) || /\.{4,}/.test(textLine) || /^\s*(Rs\.?\s*:?\s*)?[\d,]+\.\d{2}\s*$/i.test(textLine) || /^(DATE|PROGRAMME|TIME|POSI|POSITION|PRODUCT|DURA\.?|TELECAST(?: T\/BELT)?|TC_TIME|REMARKS|DURATION|ACTUAL TIME|TIME BELT|\s)+$/i.test(textLine.trim()) || /^- PAGE -\s*\d+$/i.test(textLine.trim())) {
        return;
      }
      const dateMatch = textLine.match(/(\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|\d{1,2}[-/][a-zA-ZЀ-ӿ]{3}[-/]\d{2,4})/);
      if (dateMatch) {
        extractedRecords.push({ mainLine: line, allItems: [...line] });
      } else if (extractedRecords.length > 0 && textLine.length > 2) {
        extractedRecords[extractedRecords.length - 1].allItems.push(...line);
      }
    });

    const parsedData = [];
    extractedRecords.forEach(record => {
      const mainTextLine = record.mainLine.map(i => i.text).join(' ');
      const allTextLine  = record.allItems.map(i => i.text).join(' ');
      const dateMatch = mainTextLine.match(/(\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|\d{1,2}[-/][a-zA-ZЀ-ӿ]{3}[-/]\d{2,4})/);
      const allTimes = [...allTextLine.matchAll(/(\d{1,2}[:.]\d{2}[:.]\d{2}(?:[:.\d]*)|\d{1,2}[:.]\d{2}\s*[APMampm]{2})/g)].map(m => m[1]);
      if (!(dateMatch && allTimes.length > 0)) return;

      /* Canonical output is ALWAYS day-first "D/M/YYYY" — that is what
         dmyToIso() on the Upload TC page and the tc_pdf_convert save endpoint
         expect. Sri Lankan TCs print dates day-first (DD/MM or DD-Mon), so no
         per-channel swapping; if a part can't be a month we swap as a
         safety net (same inference the server-side generic converter does). */
      let rawDate = dateMatch[1], date = '';
      if (/[a-zA-ZЀ-ӿ]{3}/.test(rawDate)) {
        const monthMap = {jan:1,feb:2,mar:3,apr:4,may:5,jun:6,jul:7,aug:8,sep:9,oct:10,nov:11,dec:12,'арг':4};
        let parts = rawDate.split(/[-/]/);
        let mStr = parts[1].toLowerCase();
        let yearStr = parts[2]; if (yearStr.length === 2) yearStr = '20' + yearStr;
        date = `${parseInt(parts[0], 10)}/${monthMap[mStr] || 1}/${yearStr}`;
      } else {
        let dp = rawDate.split(/[-/]/);
        let year = dp[2]; if (year.length === 2) year = '20' + year;
        let dayPart = parseInt(dp[0], 10), monPart = parseInt(dp[1], 10);
        if (monPart > 12 && dayPart <= 12) { const t = dayPart; dayPart = monPart; monPart = t; }
        date = `${dayPart}/${monPart}/${year}`;
      }

      let time = '', duration = '', durationTime, advtTime;
      if (normalizedChannel === 'Supreme TV') {
        durationTime = allTimes.find(t => t.startsWith('0:00:'));
        advtTime     = allTimes.find(t => !t.startsWith('0:00:')) || allTimes[0];
      } else {
        durationTime = allTimes.find(t => t.startsWith('00:00:'));
        if (normalizedChannel === 'Star Tamil' || normalizedChannel === 'Siyatha TV') {
          advtTime = allTimes.find(t => /\d{2}[:.]\d{2}[:.]\d{2}/.test(t) && !t.startsWith('00:00:'))
                  || allTimes.find(t => /[AP]M/i.test(t))
                  || allTimes.find(t => !t.startsWith('00:00:')) || allTimes[0];
        } else {
          advtTime = allTimes.find(t => !t.startsWith('00:00:')) || allTimes[0];
        }
      }

      if (advtTime) {
        if (/[AP]M/i.test(advtTime)) {
          let m = advtTime.match(/(\d{1,2})[:.](\d{2})\s*([AP]M)/i);
          if (m) {
            let h = parseInt(m[1],10);
            if (m[3].toUpperCase() === 'PM' && h < 12) h += 12;
            if (m[3].toUpperCase() === 'AM' && h === 12) h = 0;
            time = `${h.toString().padStart(2,'0')}:${m[2]}:00`;
          } else { time = advtTime; }
        } else { time = advtTime.substring(0, 8); }
      }

      if (durationTime) {
        if (normalizedChannel === 'Supreme TV') {
          duration = parseInt(durationTime.replace('0:00:', ''), 10).toString();
        } else {
          duration = durationTime.substring(0, 8).replace('00:00:', '');
        }
      } else {
        let ti = allTextLine.search(/\b\d{2}:\d{2}:\d{2}/);
        if (ti > 0) {
          let lw = allTextLine.substring(0, ti).trim().match(/\b(\d+)\b$/);
          if (lw && ['5','10','15','20','25','30','45','60'].includes(lw[1])) duration = lw[1];
        }
        if (!duration) {
          const dm = allTextLine.match(/\b(\d{2})\b(?=\s*$)/) || allTextLine.match(/\b(10|15|20|25|30)\b/);
          if (dm) duration = dm[1];
        }
      }

      let remainingItems = [];
      record.allItems.forEach(item => {
        let c = item.text.trim();
        let isSrNo = /^\d{1,4}$/.test(c) && item.x < 50;
        let isCutNo = /^(ITNCOMM|VTVCOMM|ITNOPCL)\w+$/i.test(c) || /^\d{5,}$/.test(c);
        if (c === duration || (durationTime && c === durationTime)) return;
        if (isSrNo || isCutNo) return;
        c = c.replace(rawDate, '').trim();
        c = c.replace(/\d{1,2}[:.]\d{2}[:.]\d{2}(?:[:.\d]*)/g, '').trim();
        c = c.replace(/\d{1,2}[:.]\d{2}\s*[APMampm]{2}/gi, '').trim();
        c = c.replace(/\d{2}[:.]\d{2}(?:[:.]\d{2})?\s*-\s*\d{2}[:.]\d{2}(?:[:.]\d{2})?/g, '').trim();
        c = c.replace(/\b(CB|FCT|SPO)\b/gi, '').trim();
        c = c.replace(/Certificate of Transmission/gi, '').trim();
        c = c.replace(/Voice of Asia(?: Network)?(?:\s*\(Pvt\)\s*Ltd\.?)?/gi, '').trim();
        c = c.replace(/This is a computer generated(?: Transmission certificate(?: no signature is required\.?)?)?/gi, '').trim();
        c = c.replace(/Transmission certificate(?: no signature is required\.?)?/gi, '').trim();
        c = c.replace(/Page\s*\d+/gi, '').trim();
        c = c.replace(/J Lanka Group Of Companies/gi, '').trim();
        c = c.replace(/Actual time/gi, '').trim();
        c = c.replace(/Time Belt/gi, '').trim();
        // Stripping times above can leave empty "( )" behind, e.g. "TELE DRAMA (9:30 PM)"
        c = c.replace(/\(\s*\)/g, '').trim();
        if (c.length <= 1) return;
        // Standalone time-belt tokens like "9PM" (a separate column on some TCs)
        if (/^\d{1,2}(?:[:.]\d{2})?\s*(?:AM|PM)$/i.test(c)) return;
        if (/^(Mon|Tue|Wed|We|Thu|Fri|Sat|Sun|BS|PO|PD|OTHERS|Rs\.?:?|[STE]|AM|PM)$/i.test(c)) return;
        if (/^(DATE|PROGRAMME|TIME|POSI|POSITION|PRODUCT|DURA\.?|TELECAST(?: T\/BELT)?|TC_TIME|REMARKS|DURATION|ACTUAL TIME|TIME BELT|\s)+$/i.test(c)) return;
        if (/^CB\s*\d*$/i.test(c)) return;
        if (/^[\d,]+\.\d+$/.test(c)) return;
        if (/^\.+$/.test(c)) return;
        remainingItems.push({ ...item, text: c });
      });

      let program = '', commercial = '';
      if (normalizedChannel === 'Supreme TV') {
        commercial = remainingItems.map(i => i.text).join(' ').trim();
      } else if (remainingItems.length > 0) {
        let sortedItems = [...remainingItems].sort((a, b) => a.x - b.x);
        let maxGap = 0, splitIndex = -1;
        for (let i = 0; i < sortedItems.length - 1; i++) {
          let gap = sortedItems[i+1].x - sortedItems[i].endX;
          if (gap > maxGap) { maxGap = gap; splitIndex = i; }
        }
        let commXThreshold = (maxGap > 4 && splitIndex !== -1) ? sortedItems[splitIndex].endX + (maxGap / 2) : 9999;
        let programParts = [], commercialParts = [];
        remainingItems.forEach(item => {
          if (item.x >= commXThreshold) { commercialParts.push(item.text); return; }
          let isCrossed = commXThreshold !== 9999 && item.endX > commXThreshold + 5;
          const sm = item.text.match(/^(.*?)\s+-\s*(.*)$/);
          if (sm && (commXThreshold === 9999 || isCrossed) && sm[1].trim().length > 2 && sm[2].trim().length > 2) {
            programParts.push(sm[1].trim()); commercialParts.push(sm[2].trim());
          } else { programParts.push(item.text); }
        });
        program = programParts.join(' ').trim();
        commercial = commercialParts.join(' ').trim();
        if (program.endsWith('-')) program = program.slice(0, -1).trim();
        if (commercial.startsWith('-')) commercial = commercial.slice(1).trim();
        if (program && !commercial) {
          const sm = program.match(/^(.*?)\s+-\s*(.*)$/);
          if (sm && sm[1].trim().length > 2) { program = sm[1].trim(); commercial = sm[2].trim(); }
        } else if (!program && commercial) {
          const sm = commercial.match(/^(.*?)\s+-\s*(.*)$/);
          if (sm && sm[1].trim().length > 2) { program = sm[1].trim(); commercial = sm[2].trim(); }
        }
        if (duration && commercial.endsWith(duration)) commercial = commercial.slice(0, -duration.length).trim();
        // Belt token fused into the theme text ("9PM MOBITEL SLT - …")
        commercial = commercial.replace(/^\d{1,2}(?:[:.]\d{2})?\s*(?:AM|PM)\b\s*/i, '').trim();
        // Theme wrapped onto a lost continuation line leaves a dangling dash
        // ("MOBITEL SLT -") — trim it so the theme still groups/maps cleanly.
        if (commercial.endsWith('-')) commercial = commercial.slice(0, -1).trim();
      }

      if (program || commercial) {
        parsedData.push({
          "Date":       date,
          "Programme":  normalizedChannel === 'Supreme TV' ? 'N/A' : program.trim(),
          "Aired Time": time,
          "TC Theme":   commercial.trim(),
          "Duration":   duration,
        });
      }
    });

    return parsedData;
  }

  global.TCPDFParser = { CHANNEL_MAP, normalizeChannel, parsePDF };

})(window);
