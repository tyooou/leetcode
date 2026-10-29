// leetcode-cookie-helper
//
// Copies LeetCode's LEETCODE_SESSION + csrftoken cookies out of Safari's
// cookie jar into the login Keychain, where sync.py reads them.
//
// Run by launchd (com.tyou.leetcode-cookies.plist) whenever Safari's
// Cookies/ directory changes. Safari rewrites it for every site's cookies,
// not just LeetCode's, so most runs find nothing new and exit silently.
//
// Security notes -- this binary holds a Full Disk Access grant, and the
// file it reads holds every site's session cookies:
//   * Everything it touches is a compile-time constant. It reads no
//     arguments or environment variables, so the FDA grant can't be
//     repurposed to read other files or other sites' cookies.
//   * No network code.
//   * The cookie file is parsed as untrusted input: size cap, every offset
//     bounds-checked, and only LeetCode cookies ever have their values
//     decoded.
//   * Secrets go to the Keychain through the Security framework -- never
//     through argv (visible in `ps`) -- and are never logged, not even
//     their lengths.
//   * Values are restricted to RFC 6265 cookie-octets before storing,
//     since sync.py interpolates them straight into a Cookie header.
//
// Output: stdout gets every outcome (handy when run by hand; launchd
// discards it). stderr -- cookie-helper.log under launchd -- only gets
// state changes and errors, so the frequent no-op runs don't flood it.
//
// Build + install. Re-grant Full Disk Access after every rebuild: the
// grant is pinned to the binary's code signature, so a swapped or
// modified binary loses it.
//   dir=~/Library/Application\ Support/leetcode-sync/bin
//   mkdir -p "$dir" && chmod 700 "$dir" "$dir/.."
//   swiftc -O main.swift -o "$dir/leetcode-cookie-helper"
//   codesign -s - --force "$dir/leetcode-cookie-helper"

import Foundation
import Security

enum K {
    static let home: String = {
        // getpwuid, not $HOME: the environment is not an input.
        if let pw = getpwuid(getuid()), let dir = pw.pointee.pw_dir {
            return String(cString: dir)
        }
        return "/var/empty"
    }()
    static let cookieFile = home + "/Library/Containers/com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies"
    static let lastCookieNotify = home + "/dev/leetcode/.leetcode-sync/.last_cookie_notify"

    static let keychainService = "com.tyou.leetcode-sync"
    static let syncAgentLabel = "com.tyou.leetcode-sync"
    static let domains: Set<String> = ["leetcode.com", ".leetcode.com"]
    static let names = ["LEETCODE_SESSION", "csrftoken"]

    static let maxFileSize = 64 * 1024 * 1024
    static let maxValueLength = 8192
}

// MARK: - Output

func timestamp() -> String {
    let f = DateFormatter()
    f.dateFormat = "yyyy-MM-dd HH:mm:ss"
    return f.string(from: Date())
}

/// Every outcome, for manual runs.
func say(_ message: String) {
    print(message)
}

/// State changes and errors -- lands in cookie-helper.log under launchd.
func log(_ message: String) {
    say(message)
    FileHandle.standardError.write(Data("\(timestamp()) \(message)\n".utf8))
}

/// launchd creates StandardErrorPath with default (world-readable) perms.
/// Nothing secret is logged, but keep it owner-only anyway.
func lockDownLog() {
    var st = stat()
    if fstat(STDERR_FILENO, &st) == 0, (st.st_mode & S_IFMT) == S_IFREG {
        fchmod(STDERR_FILENO, 0o600)
    }
}

// MARK: - Reading the cookie file

enum ReadError: Error {
    case open(Int32)
    case notRegularFile
    case badSize
    case shortRead
}

/// Whole file in one go, so parsing works on a single snapshot rather than
/// re-reading a file Safari may be rewriting underneath us.
func readCookieFile() throws -> [UInt8] {
    let fd = open(K.cookieFile, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
    guard fd >= 0 else { throw ReadError.open(errno) }
    defer { close(fd) }

    var st = stat()
    guard fstat(fd, &st) == 0, (st.st_mode & S_IFMT) == S_IFREG else {
        throw ReadError.notRegularFile
    }
    guard st.st_size > 0, st.st_size <= K.maxFileSize else { throw ReadError.badSize }

    var buf = [UInt8](repeating: 0, count: Int(st.st_size))
    var got = 0
    while got < buf.count {
        let n = buf.withUnsafeMutableBytes { read(fd, $0.baseAddress! + got, $0.count - got) }
        if n < 0 {
            if errno == EINTR { continue }
            throw ReadError.shortRead
        }
        if n == 0 { break }
        got += n
    }
    guard got == buf.count else { throw ReadError.shortRead }  // shrank mid-read
    return buf
}

// MARK: - Parsing Safari's binarycookies format

/// Bounds-checked view over the file. Every accessor returns nil rather
/// than reading past the end, so malformed input can't crash the parser.
struct Bytes {
    let b: [UInt8]

    func range(_ offset: Int, _ length: Int) -> Range<Int>? {
        guard offset >= 0, length >= 0 else { return nil }
        let (end, overflow) = offset.addingReportingOverflow(length)
        guard !overflow, end <= b.count else { return nil }
        return offset..<end
    }

    func u32be(_ offset: Int) -> Int? {
        guard let r = range(offset, 4) else { return nil }
        return b[r].reduce(0) { $0 << 8 | Int($1) }
    }

    func u32le(_ offset: Int) -> Int? {
        guard let r = range(offset, 4) else { return nil }
        return b[r].reversed().reduce(0) { $0 << 8 | Int($1) }
    }

    func f64le(_ offset: Int) -> Double? {
        guard let r = range(offset, 8) else { return nil }
        return Double(bitPattern: b[r].reversed().reduce(UInt64(0)) { $0 << 8 | UInt64($1) })
    }

    /// NUL-terminated string at `offset`; the NUL must come before `limit`.
    func cString(_ offset: Int, limit: Int) -> [UInt8]? {
        guard offset >= 0, offset < limit, limit <= b.count,
              let nul = b[offset..<limit].firstIndex(of: 0) else { return nil }
        return Array(b[offset..<nul])
    }
}

enum ParseError: Error {
    case badHeader
    case badPage
    case badCookie
}

struct Cookie {
    let value: [UInt8]
    let expires: Date
}

/// Layout: big-endian header ("cook", page count, page sizes), then pages
/// of little-endian cookie records. Returns only the wanted LeetCode
/// cookies, keeping the latest-expiring one per name.
func parseLeetCodeCookies(_ data: [UInt8]) throws -> [String: Cookie] {
    let f = Bytes(b: data)
    guard data.count >= 8, data[0..<4].elementsEqual("cook".utf8),
          let pageCount = f.u32be(4), pageCount <= data.count / 4,
          let sizeTable = f.range(8, pageCount * 4) else { throw ParseError.badHeader }

    var found: [String: Cookie] = [:]
    var pageStart = sizeTable.upperBound
    for i in 0..<pageCount {
        guard let size = f.u32be(8 + 4 * i),
              let page = f.range(pageStart, size) else { throw ParseError.badPage }
        try parsePage(f, page, into: &found)
        pageStart = page.upperBound
    }
    return found
}

func parsePage(_ f: Bytes, _ page: Range<Int>, into found: inout [String: Cookie]) throws {
    let p = page.lowerBound
    guard f.u32be(p) == 0x0000_0100, let count = f.u32le(p + 4),
          count <= page.count / 4, p + 8 + 4 * count <= page.upperBound else {
        throw ParseError.badPage
    }

    for j in 0..<count {
        // Record header: size, 3 unknown/flag words, then offsets (relative
        // to the record) of domain, name, path, value; expiry at +40 as
        // seconds since 2001-01-01.
        guard let rel = f.u32le(p + 8 + 4 * j),
              let size = f.u32le(p + rel), size >= 56,
              let record = f.range(p + rel, size), record.upperBound <= page.upperBound,
              let domainOffset = f.u32le(record.lowerBound + 16),
              let nameOffset = f.u32le(record.lowerBound + 20),
              let valueOffset = f.u32le(record.lowerBound + 28),
              let expiry = f.f64le(record.lowerBound + 40),
              let domain = f.cString(record.lowerBound + domainOffset, limit: record.upperBound),
              let name = f.cString(record.lowerBound + nameOffset, limit: record.upperBound)
        else { throw ParseError.badCookie }

        // Filter before touching the value: other sites' secrets are never
        // decoded, let alone stored.
        guard K.domains.contains(String(decoding: domain, as: UTF8.self)),
              let key = K.names.first(where: { name.elementsEqual($0.utf8) }) else { continue }

        guard expiry.isFinite,
              let value = f.cString(record.lowerBound + valueOffset, limit: record.upperBound)
        else { throw ParseError.badCookie }

        let expires = Date(timeIntervalSinceReferenceDate: expiry)
        if let previous = found[key], previous.expires >= expires { continue }
        found[key] = Cookie(value: value, expires: expires)
    }
}

/// One retry, in case Safari was mid-write.
func loadCookies() -> [String: Cookie]? {
    for attempt in 1...2 {
        do {
            return try parseLeetCodeCookies(try readCookieFile())
        } catch ReadError.open(let code) {
            if code == EPERM || code == EACCES {
                log("cannot read Safari cookies: grant this helper Full Disk Access")
            } else {
                log("cannot open Safari cookies (errno \(code))")
            }
            return nil
        } catch {
            if attempt == 2 {
                log("parse error (\(error))")
            } else {
                sleep(2)
            }
        }
    }
    return nil
}

/// RFC 6265 cookie-octet: printable ASCII minus space, quote, comma,
/// semicolon and backslash. Blocks header injection into sync.py's Cookie
/// header.
func isCookieSafe(_ value: [UInt8]) -> Bool {
    let banned: Set<UInt8> = [0x22, 0x2C, 0x3B, 0x5C]
    return !value.isEmpty && value.count <= K.maxValueLength
        && value.allSatisfy { (0x21...0x7E).contains($0) && !banned.contains($0) }
}

// MARK: - Keychain

func keychainQuery(_ account: String) -> [String: Any] {
    [kSecClass as String: kSecClassGenericPassword,
     kSecAttrService as String: K.keychainService,
     kSecAttrAccount as String: account]
}

func keychainRead(_ account: String) -> (OSStatus, Data?) {
    var query = keychainQuery(account)
    query[kSecReturnData as String] = true
    query[kSecMatchLimit as String] = kSecMatchLimitOne
    var out: CFTypeRef?
    let status = SecItemCopyMatching(query as CFDictionary, &out)
    return (status, out as? Data)
}

func keychainWrite(_ account: String, _ value: Data) -> OSStatus {
    let query = keychainQuery(account)
    let status = SecItemUpdate(query as CFDictionary, [kSecValueData as String: value] as CFDictionary)
    guard status == errSecItemNotFound else { return status }
    var add = query
    add[kSecValueData as String] = value
    add[kSecAttrLabel as String] = "LeetCode sync: \(account)"
    return SecItemAdd(add as CFDictionary, nil)
}

// MARK: - Main

/// Run the sync now instead of waiting for its next 30-min poll. A no-op
/// if the agent isn't loaded.
func kickSync() {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
    p.arguments = ["kickstart", "gui/\(getuid())/\(K.syncAgentLabel)"]
    p.standardOutput = FileHandle.nullDevice
    p.standardError = FileHandle.nullDevice
    do {
        try p.run()
        p.waitUntilExit()
    } catch {}
}

func main() -> Int32 {
    lockDownLog()
    guard let found = loadCookies() else { return 1 }

    var fresh: [String: Data] = [:]
    for name in K.names {
        guard let cookie = found[name] else {
            say("not found (\(name)) -- log in to leetcode.com in Safari")
            return 0
        }
        guard cookie.expires > Date() else {
            say("expired (\(name)) -- log in to leetcode.com in Safari")
            return 0
        }
        guard isCookieSafe(cookie.value) else {
            log("rejected \(name): unexpected characters in value")
            return 1
        }
        fresh[name] = Data(cookie.value)
    }

    var changed = false
    for name in K.names {
        let value = fresh[name]!
        let (status, stored) = keychainRead(name)
        if status == errSecSuccess, stored == value { continue }
        if status != errSecSuccess, status != errSecItemNotFound {
            log("keychain read failed for \(name) (OSStatus \(status))")
            return 1
        }
        let writeStatus = keychainWrite(name, value)
        guard writeStatus == errSecSuccess else {
            log("keychain write failed for \(name) (OSStatus \(writeStatus))")
            return 1
        }
        changed = true
    }

    guard changed else {
        say("unchanged")
        return 0
    }

    unlink(K.lastCookieNotify)  // let the next expiry notify immediately
    let expires = found["LEETCODE_SESSION"]!.expires
    let day = DateFormatter()
    day.dateFormat = "yyyy-MM-dd"
    log("updated (expires \(day.string(from: expires)))")
    kickSync()
    return 0
}

exit(main())
