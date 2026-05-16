/*
 * protocol.h — line-based wire protocol for schwung-testd.
 *
 * The daemon speaks a tiny ASCII protocol: one command per line, one
 * response per line. Replies start with `OK` or `ERR`. The functions
 * here handle the byte-level concerns (read a line, send a line, parse
 * hex) so command handlers can stay focused on semantics.
 *
 * Verb dispatch lives in commands.c — protocol.c is intentionally
 * unaware of the specific commands the daemon supports, so adding new
 * commands never touches transport code.
 */

#ifndef SCHWUNG_TESTD_PROTOCOL_H
#define SCHWUNG_TESTD_PROTOCOL_H

#include <stddef.h>
#include <stdint.h>

#define TESTD_LINE_MAX 256

/* ---- transport (line I/O) ---------------------------------------------- */

/* read_line — block until a complete `\n`-terminated line is read into
 * out (NUL-terminated, max cap-1 bytes, trailing CR stripped).
 *
 * Returns the line length on success, -1 on:
 *   - peer closed the connection
 *   - I/O error
 *   - line exceeds cap (protocol violation)
 *   - embedded NUL in the line (would lossy-truncate the dispatcher) */
int protocol_read_line(int fd, char *out, size_t cap);

/* send_line / reply: write a single line to fd, append \n.
 * reply() takes the line without trailing newline. */
int protocol_send_line(int fd, const char *s);
int protocol_reply(int fd, const char *line);
int protocol_reply_err(int fd, const char *msg);

/* ---- hex codec --------------------------------------------------------- */

/* parse_hex: decode `len_chars` hex chars (must be even) into len_chars/2
 * bytes. Returns 0 on success, -1 on bad input. */
int protocol_parse_hex(const char *s, size_t len_chars, uint8_t *out);

/* format_hex: encode n bytes as 2*n lowercase hex chars + NUL.
 * out must hold at least 2*n + 1 bytes. */
void protocol_format_hex(const uint8_t *bytes, size_t n, char *out);

/* ---- command line parsing --------------------------------------------- */

/* split_command: in-place split of a command line into verb + args.
 * Verb is uppercased (commands are case-insensitive on input). Args is
 * NULL if no arguments were present, otherwise points into `line` past
 * the first whitespace run. */
void protocol_split_command(char *line, char **verb, char **args);

#endif /* SCHWUNG_TESTD_PROTOCOL_H */
