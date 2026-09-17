import { open } from "node:fs/promises";

// Incremental, bounded reads of this agent's exact transcript, never native history scans.
export class TranscriptReceipts {
  private offset = 0;
  private pending = Buffer.alloc(0);
  private inode: number | undefined;
  private header = false;
  private ids = new Set<string>();
  private failure: Error | undefined;

  async scan(path: string, nativeId: string) {
    if (this.failure) throw this.failure;
    let file;
    try { file = await open(path, "r"); }
    catch (error: any) {
      if (error.code === "ENOENT") return { ids: new Set<string>(), complete: true };
      throw error;
    }
    try {
      const stat = await file.stat();
      if (this.inode !== undefined && (this.inode !== stat.ino || stat.size < this.offset)) {
        throw new Error("Native transcript replaced or truncated; restart bridge after checking session identity");
      }
      this.inode = stat.ino;
      let budget = 8 * 1024 * 1024;
      while (this.offset < stat.size && budget > 0) {
        const buffer = Buffer.alloc(Math.min(64 * 1024, stat.size - this.offset, budget));
        const { bytesRead } = await file.read(buffer, 0, buffer.length, this.offset);
        if (!bytesRead) break;
        this.offset += bytesRead;
        budget -= bytesRead;
        this.pending = Buffer.concat([this.pending, buffer.subarray(0, bytesRead)]);
        let end;
        while ((end = this.pending.indexOf(10)) !== -1) {
          const line = this.pending.subarray(0, end).toString("utf8");
          this.pending = this.pending.subarray(end + 1);
          const entry = JSON.parse(line);
          if (!this.header) {
            if (entry.type !== "session" || entry.id !== nativeId) throw new Error("Native transcript identity mismatch");
            this.header = true;
          }
          if (entry.type === "custom_message" && entry.customType === "multiverse-inbox") {
            for (const id of entry.details?.ids ?? []) if (typeof id === "string") this.ids.add(id);
          }
        }
        if (this.pending.length > 8 * 1024 * 1024) throw new Error("Native transcript record exceeds bridge read limit");
      }
      return { ids: new Set(this.ids), complete: this.offset >= stat.size && this.pending.length === 0 };
    } catch (error: any) {
      this.failure = error;
      throw error;
    } finally { await file.close(); }
  }
}
