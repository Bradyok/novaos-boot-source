// SPDX-License-Identifier: GPL-2.0+
/*
 * Opens a loader file the way `rkdeveloptool db` does (main.cpp download_boot: new
 * CRKImage(file) -> m_bootObject, the CRC and tag checks of RKBoot.cpp) and writes the 471 and
 * 472 entries exactly as DownloadBoot would send them (RKDevice.cpp: GetEntryProperty +
 * GetEntryData). Linked against the pinned rkdeveloptool's own RKImage.cpp, RKBoot.cpp and crc.cpp
 * by build.sh; nothing here touches USB (libusb.h is a stub on the include path).
 *
 *     ldr_check LOADER OUTDIR    -> OUTDIR/471_0.bin, OUTDIR/472_0.bin (still RC4'd, as sent)
 *
 * Copyright (C) 2026 Olympian LED, Inc.
 */
#include "RKImage.h"

static int dump(CRKBoot *boot, ENUM_RKBOOTENTRY type, const char *tag, unsigned char count, const char *dir)
{
	for (unsigned char i = 0; i < count; i++) {
		DWORD size = 0, delay = 0;
		if (!boot->GetEntryProperty(type, i, size, delay))
			return 1;
		PBYTE buf = new BYTE[size];
		if (!boot->GetEntryData(type, i, buf))
			return 1;
		char path[4096];
		snprintf(path, sizeof(path), "%s/%s_%u.bin", dir, tag, i);
		FILE *f = fopen(path, "wb");
		if (!f || fwrite(buf, 1, size, f) != size)
			return 1;
		fclose(f);
		delete[] buf;
		printf("%s[%u]: %u bytes, delay %u ms\n", tag, i, size, delay);
	}
	return 0;
}

int main(int argc, char **argv)
{
	if (argc != 3) {
		fprintf(stderr, "usage: ldr_check LOADER OUTDIR\n");
		return 2;
	}
	bool ok = false;
	CRKImage image(argv[1], ok);
	if (!ok || !image.m_bootObject) {
		printf("FAIL: rkdeveloptool refuses %s\n", argv[1]);
		return 1;
	}
	CRKBoot *boot = (CRKBoot *)image.m_bootObject;
	unsigned char n471 = boot->Entry471Count, n472 = boot->Entry472Count, nl = boot->EntryLoaderCount;
	printf("accepted: support_device 0x%x, 471 x%u, 472 x%u, loader x%u, rc4_disable %d\n",
	       (unsigned)boot->SupportDevice, n471, n472, nl, (int)(bool)boot->Rc4DisableFlag);
	if (dump(boot, ENTRY471, "471", n471, argv[2]) || dump(boot, ENTRY472, "472", n472, argv[2])) {
		printf("FAIL: an entry could not be read\n");
		return 1;
	}
	return 0;
}
