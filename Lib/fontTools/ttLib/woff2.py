from __future__ import print_function, division, absolute_import
from fontTools.misc.py23 import *
import sys
import array
import struct
from collections import OrderedDict
from fontTools.misc import sstruct
from fontTools.misc.arrayTools import calcIntBounds
from fontTools.misc.textTools import pad
from fontTools.ttLib import (TTFont, TTLibError, getTableModule, getTableClass,
	getSearchRange)
from fontTools.ttLib.sfnt import (SFNTReader, SFNTWriter, DirectoryEntry,
	WOFFFlavorData, sfntDirectoryFormat, sfntDirectorySize, SFNTDirectoryEntry,
	sfntDirectoryEntrySize, calcChecksum)
from fontTools.ttLib.tables import ttProgram

haveBrotli = False
try:
	import brotli
	haveBrotli = True
except ImportError:
	pass


class WOFF2Reader(SFNTReader):

	flavor = "woff2"

	def __init__(self, file, checkChecksums=1, fontNumber=-1):
		if not haveBrotli:
			print('The WOFF2 decoder requires the Brotli Python extension, available at:\n'
				  'https://github.com/google/brotli', file=sys.stderr)
			raise ImportError("No module named brotli")

		self.file = file

		signature = Tag(self.file.read(4))
		if signature != b"wOF2":
			raise TTLibError("Not a WOFF2 font (bad signature)")

		self.file.seek(0)
		self.DirectoryEntry = WOFF2DirectoryEntry
		data = self.file.read(woff2DirectorySize)
		if len(data) != woff2DirectorySize:
			raise TTLibError('Not a WOFF2 font (not enough data)')
		sstruct.unpack(woff2DirectoryFormat, data, self)

		self.tables = OrderedDict()
		offset = 0
		for i in range(self.numTables):
			entry = self.DirectoryEntry()
			entry.fromFile(self.file)
			tag = Tag(entry.tag)
			self.tables[tag] = entry
			entry.offset = offset
			offset += entry.length

		totalUncompressedSize = offset
		compressedData = self.file.read(self.totalCompressedSize)
		decompressedData = brotli.decompress(compressedData)
		if len(decompressedData) != totalUncompressedSize:
			raise TTLibError(
				'unexpected size for decompressed font data: expected %d, found %d'
				% (totalUncompressedSize, len(decompressedData)))
		self.transformBuffer = BytesIO(decompressedData)

		self.file.seek(0, 2)
		if self.length != self.file.tell():
			raise TTLibError("reported 'length' doesn't match the actual file size")

		self.flavorData = WOFF2FlavorData(self)

		# make empty TTFont to store data while reconstructing tables
		self.ttFont = TTFont(recalcBBoxes=False, recalcTimestamp=False)

	def __getitem__(self, tag):
		"""Fetch the raw table data. Reconstruct transformed tables."""
		entry = self.tables[Tag(tag)]
		if not hasattr(entry, 'data'):
			if tag in woff2TransformedTableTags:
				entry.data = self.reconstructTable(tag)
			else:
				entry.data = entry.loadData(self.transformBuffer)
		return entry.data

	def reconstructTable(self, tag):
		"""Reconstruct table named 'tag' from transformed data."""
		if tag not in woff2TransformedTableTags:
			raise TTLibError("transform for table '%s' is unknown" % tag)
		entry = self.tables[Tag(tag)]
		rawData = entry.loadData(self.transformBuffer)
		if tag == 'glyf':
			# no need to pad glyph data when reconstructing
			padding = self.padding if hasattr(self, 'padding') else None
			data = self._reconstructGlyf(rawData, padding)
		elif tag == 'loca':
			data = self._reconstructLoca()
		else:
			raise NotImplementedError
		return data

	def _reconstructGlyf(self, data, padding=None):
		""" Return recostructed glyf table data, and set the corresponding loca's
		locations. Optionally pad glyph offsets to the specified number of bytes.
		"""
		self.ttFont['loca'] = WOFF2LocaTable()
		glyfTable = self.ttFont['glyf'] = WOFF2GlyfTable()
		glyfTable.reconstruct(data, self.ttFont)
		if padding:
			glyfTable.padding = padding
		data = glyfTable.compile(self.ttFont)
		return data

	def _reconstructLoca(self):
		""" Return reconstructed loca table data. """
		if 'loca' not in self.ttFont:
			# make sure glyf is reconstructed first
			self.tables['glyf'].data = self.reconstructTable('glyf')
		locaTable = self.ttFont['loca']
		data = locaTable.compile(self.ttFont)
		if len(data) != self.tables['loca'].origLength:
			raise TTLibError(
				"reconstructed 'loca' table doesn't match original size: "
				"expected %d, found %d"
				% (self.tables['loca'].origLength, len(data)))
		return data


class WOFF2Writer(SFNTWriter):

	flavor = "woff2"

	def __init__(self, file, numTables, sfntVersion="\000\001\000\000",
		         flavor=None, flavorData=None):
		if not haveBrotli:
			print('The WOFF2 encoder requires the Brotli Python extension, available at:\n'
				  'https://github.com/google/brotli', file=sys.stderr)
			raise ImportError("No module named brotli")

		self.file = file
		self.numTables = numTables
		self.sfntVersion = Tag(sfntVersion)
		self.flavorData = flavorData or WOFF2FlavorData()

		self.directoryFormat = woff2DirectoryFormat
		self.directorySize = woff2DirectorySize
		self.DirectoryEntry = WOFF2DirectoryEntry

		self.signature = Tag("wOF2")

		self.nextTableOffset = 0
		self.transformBuffer = BytesIO()

		self.tables = OrderedDict()

		# make empty TTFont to store data while normalising and transforming tables
		self.ttFont = TTFont(recalcBBoxes=False, recalcTimestamp=False)

	def __setitem__(self, tag, data):
		"""Associate new entry named 'tag' with raw table data."""
		if tag in self.tables:
			raise TTLibError("cannot rewrite '%s' table" % tag)
		if tag == 'DSIG':
			# always drop DSIG table, since the encoding process can invalidate it
			self.numTables -= 1
			return

		entry = self.DirectoryEntry()
		entry.tag = Tag(tag)
		entry.flags = getKnownTagIndex(entry.tag)
		# WOFF2 table data are written to disk only on close(), after all tags
		# have been specified
		entry.data = data

		self.tables[tag] = entry

	def close(self):
		""" All tags must have been specified. Now write the table data and directory.
		"""
		if len(self.tables) != self.numTables:
			raise TTLibError("wrong number of tables; expected %d, found %d" % (self.numTables, len(self.tables)))

		if self.sfntVersion in ("\x00\x01\x00\x00", "true"):
			isTrueType = True
		elif self.sfntVersion == "OTTO":
			isTrueType = False
		else:
			raise TTLibError("Not a TrueType or OpenType font (bad sfntVersion)")

		# The WOFF2 spec no longer requires the glyph offsets to be 4-byte aligned.
		# However, the reference WOFF2 implementation still fails to reconstruct
		# 'unpadded' glyf tables, therefore we need to 'normalise' them.
		# See:
		# https://github.com/khaledhosny/ots/issues/60
		# https://github.com/google/woff2/issues/15
		if isTrueType:
			self._normaliseGlyfAndLoca(padding=4)
		self._setHeadTransformFlag()

		# To pass the legacy OpenType Sanitiser currently included in browsers,
		# we must sort the table directory and data alphabetically by tag.
		# See:
		# https://github.com/google/woff2/pull/3
		# https://lists.w3.org/Archives/Public/public-webfonts-wg/2015Mar/0000.html
		# TODO(user): remove to match spec once browsers are on newer OTS
		self.tables = OrderedDict(sorted(self.tables.items()))

		self.totalSfntSize = self._calcSFNTChecksumsLengthsAndOffsets()

		fontData = self._transformTables()
		compressedFont = brotli.compress(fontData, mode=brotli.MODE_FONT)

		self.totalCompressedSize = len(compressedFont)
		self.length = self._calcTotalSize()
		self.majorVersion, self.minorVersion = self._getVersion()
		self.reserved = 0

		directory = self._packTableDirectory()
		self.file.seek(0)
		self.file.write(pad(directory + compressedFont, size=4))
		self._writeFlavorData()

	def _normaliseGlyfAndLoca(self, padding=4):
		""" Recompile glyf and loca tables, aligning glyph offsets to multiples of
		'padding' size. Update the head table's 'indexToLocFormat' accordingly while
		compiling loca.
		"""
		if self.sfntVersion == "OTTO":
			return
		for tag in ('maxp', 'head', 'loca', 'glyf'):
			self._decompileTable(tag)
		self.ttFont['glyf'].padding = padding
		for tag in ('glyf', 'loca'):
			self._compileTable(tag)

	def _setHeadTransformFlag(self):
		""" Set bit 11 of 'head' table flags to indicate that the font has undergone
		a lossless modifying transform. Re-compile head table data."""
		self._decompileTable('head')
		self.ttFont['head'].flags |= (1 << 11)
		self._compileTable('head')

	def _decompileTable(self, tag):
		""" Fetch table data, decompile it, and store it inside self.ttFont. """
		tag = Tag(tag)
		if tag not in self.tables:
			raise TTLibError("missing required table: %s" % tag)
		if self.ttFont.isLoaded(tag):
			return
		data = self.tables[tag].data
		if tag == 'loca':
			tableClass = WOFF2LocaTable
		elif tag == 'glyf':
			tableClass = WOFF2GlyfTable
		else:
			tableClass = getTableClass(tag)
		table = tableClass(tag)
		self.ttFont.tables[tag] = table
		table.decompile(data, self.ttFont)

	def _compileTable(self, tag):
		""" Compile table and store it in its 'data' attribute. """
		self.tables[tag].data = self.ttFont[tag].compile(self.ttFont)

	def _calcSFNTChecksumsLengthsAndOffsets(self):
		""" Compute the 'original' SFNT checksums, lengths and offsets for checksum
		adjustment calculation. Return the total size of the uncompressed font.
		"""
		offset = sfntDirectorySize + sfntDirectoryEntrySize * len(self.tables)
		for tag, entry in self.tables.items():
			data = entry.data
			entry.origOffset = offset
			entry.origLength = len(data)
			if tag == 'head':
				entry.checkSum = calcChecksum(data[:8] + b'\0\0\0\0' + data[12:])
			else:
				entry.checkSum = calcChecksum(data)
			offset += (entry.origLength + 3) & ~3
		return offset

	def _transformTables(self):
		"""Return transformed font data."""
		for tag, entry in self.tables.items():
			if tag in woff2TransformedTableTags:
				data = self.transformTable(tag)
			else:
				data = entry.data
			entry.offset = self.nextTableOffset
			entry.saveData(self.transformBuffer, data)
			self.nextTableOffset += entry.length
		self.writeMasterChecksum()
		fontData = self.transformBuffer.getvalue()
		return fontData

	def transformTable(self, tag):
		"""Return transformed table data."""
		if tag not in woff2TransformedTableTags:
			raise TTLibError("Transform for table '%s' is unknown" % tag)
		if tag == "loca":
			data = b""
		elif tag == "glyf":
			for tag in ('maxp', 'head', 'loca', 'glyf'):
				self._decompileTable(tag)
			glyfTable = self.ttFont['glyf']
			data = glyfTable.transform(self.ttFont)
		else:
			raise NotImplementedError
		return data

	def _calcMasterChecksum(self):
		"""Calculate checkSumAdjustment."""
		tags = list(self.tables.keys())
		checksums = []
		for i in range(len(tags)):
			checksums.append(self.tables[tags[i]].checkSum)

		# Create a SFNT directory for checksum calculation purposes
		self.searchRange, self.entrySelector, self.rangeShift = getSearchRange(self.numTables, 16)
		directory = sstruct.pack(sfntDirectoryFormat, self)
		tables = sorted(self.tables.items())
		for tag, entry in tables:
			sfntEntry = SFNTDirectoryEntry()
			sfntEntry.tag = entry.tag
			sfntEntry.checkSum = entry.checkSum
			sfntEntry.offset = entry.origOffset
			sfntEntry.length = entry.origLength
			directory = directory + sfntEntry.toString()

		directory_end = sfntDirectorySize + len(self.tables) * sfntDirectoryEntrySize
		assert directory_end == len(directory)

		checksums.append(calcChecksum(directory))
		checksum = sum(checksums) & 0xffffffff
		# BiboAfba!
		checksumadjustment = (0xB1B0AFBA - checksum) & 0xffffffff
		return checksumadjustment

	def writeMasterChecksum(self):
		"""Write checkSumAdjustment to the transformBuffer."""
		checksumadjustment = self._calcMasterChecksum()
		self.transformBuffer.seek(self.tables['head'].offset + 8)
		self.transformBuffer.write(struct.pack(">L", checksumadjustment))

	def _calcTotalSize(self):
		"""Calculate total size of WOFF2 font, including any meta- and/or private data."""
		offset = self.directorySize
		for entry in self.tables.values():
			offset += len(entry.toString())
		offset += self.totalCompressedSize
		offset = (offset + 3) & ~3
		offset = self._calcFlavorDataOffsetsAndSize(offset)
		return offset

	def _calcFlavorDataOffsetsAndSize(self, start):
		"""Calculate offsets and lengths for any meta- and/or private data."""
		offset = start
		data = self.flavorData
		if data.metaData:
			self.metaOrigLength = len(data.metaData)
			self.metaOffset = offset
			self.compressedMetaData = brotli.compress(
				data.metaData, mode=brotli.MODE_TEXT)
			self.metaLength = len(self.compressedMetaData)
			offset += self.metaLength
		else:
			self.metaOffset = self.metaLength = self.metaOrigLength = 0
			self.compressedMetaData = b""
		if data.privData:
			# make sure private data is padded to 4-byte boundary
			offset = (offset + 3) & ~3
			self.privOffset = offset
			self.privLength = len(data.privData)
			offset += self.privLength
		else:
			self.privOffset = self.privLength = 0
		return offset

	def _getVersion(self):
		"""Return the WOFF2 font's (majorVersion, minorVersion) tuple."""
		data = self.flavorData
		if data.majorVersion is not None and data.minorVersion is not None:
			return data.majorVersion, data.minorVersion
		else:
			# if None, return 'fontRevision' from 'head' table
			if 'head' in self.tables:
				return struct.unpack(">HH", self.tables['head'].data[4:8])
			else:
				return 0, 0

	def _packTableDirectory(self):
		"""Return WOFF2 table directory data."""
		directory = sstruct.pack(self.directoryFormat, self)
		for entry in self.tables.values():
			directory = directory + entry.toString()
		return directory

	def _writeFlavorData(self):
		"""Write metadata and/or private data using appropiate padding."""
		compressedMetaData = self.compressedMetaData
		privData = self.flavorData.privData
		if compressedMetaData and privData:
			compressedMetaData = pad(compressedMetaData, size=4)
		if compressedMetaData:
			self.file.seek(self.metaOffset)
			assert self.file.tell() == self.metaOffset
			self.file.write(compressedMetaData)
		if privData:
			self.file.seek(self.privOffset)
			assert self.file.tell() == self.privOffset
			self.file.write(privData)

	def reordersTables(self):
		return True


# -- woff2 directory helpers and cruft

woff2DirectoryFormat = """
		> # big endian
		signature:           4s   # "wOF2"
		sfntVersion:         4s
		length:              L    # total woff2 file size
		numTables:           H    # number of tables
		reserved:            H    # set to 0
		totalSfntSize:       L    # uncompressed size
		totalCompressedSize: L    # compressed size
		majorVersion:        H    # major version of WOFF file
		minorVersion:        H    # minor version of WOFF file
		metaOffset:          L    # offset to metadata block
		metaLength:          L    # length of compressed metadata
		metaOrigLength:      L    # length of uncompressed metadata
		privOffset:          L    # offset to private data block
		privLength:          L    # length of private data block
"""

woff2DirectorySize = sstruct.calcsize(woff2DirectoryFormat)

woff2KnownTags = (
	"cmap", "head", "hhea", "hmtx", "maxp", "name", "OS/2", "post", "cvt ",
	"fpgm", "glyf", "loca", "prep", "CFF ", "VORG", "EBDT", "EBLC", "gasp",
	"hdmx", "kern", "LTSH", "PCLT", "VDMX", "vhea", "vmtx", "BASE", "GDEF",
	"GPOS", "GSUB", "EBSC", "JSTF", "MATH", "CBDT", "CBLC", "COLR", "CPAL",
	"SVG ", "sbix", "acnt", "avar", "bdat", "bloc", "bsln", "cvar", "fdsc",
	"feat", "fmtx", "fvar", "gvar", "hsty", "just", "lcar", "mort", "morx",
	"opbd", "prop", "trak", "Zapf", "Silf", "Glat", "Gloc", "Feat", "Sill")

woff2FlagsFormat = """
		> # big endian
		flags: B  # table type and flags
"""

woff2FlagsSize = sstruct.calcsize(woff2FlagsFormat)

woff2UnknownTagFormat = """
		> # big endian
		tag: 4s  # 4-byte tag (optional)
"""

woff2UnknownTagSize = sstruct.calcsize(woff2UnknownTagFormat)

woff2UnknownTagIndex = 0x3F

woff2Base128MaxSize = 5
woff2DirectoryEntryMaxSize = woff2FlagsSize + woff2UnknownTagSize + 2 * woff2Base128MaxSize

woff2TransformedTableTags = ('glyf', 'loca')

woff2GlyfTableFormat = """
		> # big endian
		version:                  L  # = 0x00000000
		numGlyphs:                H  # Number of glyphs
		indexFormat:              H  # Offset format for loca table
		nContourStreamSize:       L  # Size of nContour stream
		nPointsStreamSize:        L  # Size of nPoints stream
		flagStreamSize:           L  # Size of flag stream
		glyphStreamSize:          L  # Size of glyph stream
		compositeStreamSize:      L  # Size of composite stream
		bboxStreamSize:           L  # Comnined size of bboxBitmap and bboxStream
		instructionStreamSize:    L  # Size of instruction stream
"""

woff2GlyfTableFormatSize = sstruct.calcsize(woff2GlyfTableFormat)

bboxFormat = """
		>	# big endian
		xMin:				h
		yMin:				h
		xMax:				h
		yMax:				h
"""


def getKnownTagIndex(tag):
	"""Return index of 'tag' in woff2KnownTags list. Return 63 if not found."""
	for i in range(len(woff2KnownTags)):
		if tag == woff2KnownTags[i]:
			return i
	return woff2UnknownTagIndex


class WOFF2DirectoryEntry(DirectoryEntry):

	def fromFile(self, file):
		pos = file.tell()
		data = file.read(woff2DirectoryEntryMaxSize)
		left = self.fromString(data)
		consumed = len(data) - len(left)
		file.seek(pos + consumed)

	def fromString(self, data):
		if len(data) < 1:
			raise TTLibError("can't read table 'flags': not enough data")
		dummy, data = sstruct.unpack2(woff2FlagsFormat, data, self)
		if self.flags & 0x3F == 0x3F:
			# if bits [0..5] of the flags byte == 63, read a 4-byte arbitrary tag value
			if len(data) < woff2UnknownTagSize:
				raise TTLibError("can't read table 'tag': not enough data")
			dummy, data = sstruct.unpack2(woff2UnknownTagFormat, data, self)
		else:
			# otherwise, tag is derived from a fixed 'Known Tags' table
			self.tag = woff2KnownTags[self.flags & 0x3F]
		self.tag = Tag(self.tag)
		if self.flags & 0xC0 != 0:
			raise TTLibError('bits 6-7 are reserved and must be 0')
		self.origLength, data = unpackBase128(data)
		self.length = self.origLength
		if self.tag in woff2TransformedTableTags:
			self.length, data = unpackBase128(data)
			if self.tag == 'loca' and self.length != 0:
				raise TTLibError(
					"the transformLength of the 'loca' table must be 0")
		# return left over data
		return data

	def toString(self):
		data = bytechr(self.flags)
		if (self.flags & 0x3F) == 0x3F:
			data += struct.pack('>4s', self.tag.tobytes())
		data += packBase128(self.origLength)
		if self.tag in woff2TransformedTableTags:
			data += packBase128(self.length)
		return data


class WOFF2LocaTable(getTableClass('loca')):
	"""Same as parent class. The only difference is that it attempts to preserve
	the 'indexFormat' as encoded in the WOFF2 glyf table.
	"""

	def __init__(self, tag=None):
		self.tableTag = Tag(tag or 'loca')

	def compile(self, ttFont):
		try:
			max_location = max(self.locations)
		except AttributeError:
			self.set([])
			max_location = 0
		if 'glyf' in ttFont and hasattr(ttFont['glyf'], 'indexFormat'):
			# copile loca using the indexFormat specified in the WOFF2 glyf table
			indexFormat = ttFont['glyf'].indexFormat
			if indexFormat == 0:
				if max_location >= 0x20000:
					raise TTLibError("indexFormat is 0 but local offsets > 0x20000")
				if not all(l % 2 == 0 for l in self.locations):
					raise TTLibError("indexFormat is 0 but local offsets not multiples of 2")
				locations = array.array("H")
				for i in range(len(self.locations)):
					locations.append(self.locations[i] // 2)
			else:
				locations = array.array("I", self.locations)
			if sys.byteorder != "big":
				locations.byteswap()
			data = locations.tostring()
		else:
			# use the most compact indexFormat given the current glyph offsets
			data = super(WOFF2LocaTable, self).compile(ttFont)
		return data


class WOFF2GlyfTable(getTableClass('glyf')):
	"""Decoder/Encoder for WOFF2 'glyf' table transform."""

	subStreams = (
		'nContourStream', 'nPointsStream', 'flagStream', 'glyphStream',
		'compositeStream', 'bboxStream', 'instructionStream')

	def __init__(self, tag=None):
		self.tableTag = Tag(tag or 'glyf')

	def reconstruct(self, data, ttFont):
		""" Decompile transformed 'glyf' data. """
		inputDataSize = len(data)

		if inputDataSize < woff2GlyfTableFormatSize:
			raise TTLibError("not enough 'glyf' data")
		dummy, data = sstruct.unpack2(woff2GlyfTableFormat, data, self)
		offset = woff2GlyfTableFormatSize

		for stream in self.subStreams:
			size = getattr(self, stream + 'Size')
			setattr(self, stream, data[:size])
			data = data[size:]
			offset += size

		if offset != inputDataSize:
			raise TTLibError(
				"incorrect size of transformed 'glyf' table: expected %d, received %d bytes"
				% (offset, inputDataSize))

		bboxBitmapSize = ((self.numGlyphs + 31) >> 5) << 2
		bboxBitmap = self.bboxStream[:bboxBitmapSize]
		self.bboxBitmap = array.array('B', bboxBitmap)
		self.bboxStream = self.bboxStream[bboxBitmapSize:]

		self.nContourStream = array.array("h", self.nContourStream)
		if sys.byteorder != "big":
			self.nContourStream.byteswap()
		assert len(self.nContourStream) == self.numGlyphs

		if 'head' in ttFont:
			ttFont['head'].indexToLocFormat = self.indexFormat
		try:
			self.glyphOrder = ttFont.getGlyphOrder()
		except:
			self.glyphOrder = None
		if self.glyphOrder is None:
			self.glyphOrder = [".notdef"]
			self.glyphOrder.extend(["glyph%.5d" % i for i in range(1, self.numGlyphs)])
		else:
			if len(self.glyphOrder) != self.numGlyphs:
				raise TTLibError(
					"incorrect glyphOrder: expected %d glyphs, found %d" %
					(len(self.glyphOrder), self.numGlyphs))

		glyphs = self.glyphs = {}
		for glyphID, glyphName in enumerate(self.glyphOrder):
			glyph = self._decodeGlyph(glyphID)
			glyphs[glyphName] = glyph

	def transform(self, ttFont):
		""" Return transformed 'glyf' data """
		self.numGlyphs = len(self.glyphs)
		if not hasattr(self, "glyphOrder"):
			try:
				self.glyphOrder = ttFont.getGlyphOrder()
			except:
				self.glyphOrder = None
			if self.glyphOrder is None:
				self.glyphOrder = [".notdef"]
				self.glyphOrder.extend(["glyph%.5d" % i for i in range(1, self.numGlyphs)])
		if len(self.glyphOrder) != self.numGlyphs:
			raise TTLibError(
				"incorrect glyphOrder: expected %d glyphs, found %d" %
				(len(self.glyphOrder), self.numGlyphs))

		if 'maxp' in ttFont:
			ttFont['maxp'].numGlyphs = self.numGlyphs
		self.indexFormat = ttFont['head'].indexToLocFormat

		for stream in self.subStreams:
			setattr(self, stream, b"")
		bboxBitmapSize = ((self.numGlyphs + 31) >> 5) << 2
		self.bboxBitmap = array.array('B', [0]*bboxBitmapSize)

		for glyphID in range(self.numGlyphs):
			self._encodeGlyph(glyphID)

		self.bboxStream = self.bboxBitmap.tostring() + self.bboxStream
		for stream in self.subStreams:
			setattr(self, stream + 'Size', len(getattr(self, stream)))
		self.version = 0
		data = sstruct.pack(woff2GlyfTableFormat, self)
		data += bytesjoin([getattr(self, s) for s in self.subStreams])
		return data

	def _decodeGlyph(self, glyphID):
		glyph = getTableModule('glyf').Glyph()
		glyph.numberOfContours = self.nContourStream[glyphID]
		if glyph.numberOfContours == 0:
			return glyph
		elif glyph.isComposite():
			self._decodeComponents(glyph)
		else:
			self._decodeCoordinates(glyph)
		self._decodeBBox(glyphID, glyph)
		return glyph

	def _decodeComponents(self, glyph):
		data = self.compositeStream
		glyph.components = []
		more = 1
		haveInstructions = 0
		while more:
			component = getTableModule('glyf').GlyphComponent()
			more, haveInstr, data = component.decompile(data, self)
			haveInstructions = haveInstructions | haveInstr
			glyph.components.append(component)
		self.compositeStream = data
		if haveInstructions:
			self._decodeInstructions(glyph)

	def _decodeCoordinates(self, glyph):
		data = self.nPointsStream
		endPtsOfContours = []
		endPoint = -1
		for i in range(glyph.numberOfContours):
			ptsOfContour, data = unpack255UShort(data)
			endPoint += ptsOfContour
			endPtsOfContours.append(endPoint)
		glyph.endPtsOfContours = endPtsOfContours
		self.nPointsStream = data
		self._decodeTriplets(glyph)
		self._decodeInstructions(glyph)

	def _decodeInstructions(self, glyph):
		glyphStream = self.glyphStream
		instructionStream = self.instructionStream
		instructionLength, glyphStream = unpack255UShort(glyphStream)
		glyph.program = ttProgram.Program()
		glyph.program.fromBytecode(instructionStream[:instructionLength])
		self.glyphStream = glyphStream
		self.instructionStream = instructionStream[instructionLength:]

	def _decodeBBox(self, glyphID, glyph):
		haveBBox = bool(self.bboxBitmap[glyphID >> 3] & (0x80 >> (glyphID & 7)))
		if glyph.isComposite() and not haveBBox:
			raise TTLibError('no bbox values for composite glyph %d' % glyphID)
		if haveBBox:
			dummy, self.bboxStream = sstruct.unpack2(bboxFormat, self.bboxStream, glyph)
		else:
			glyph.recalcBounds(self)

	def _decodeTriplets(self, glyph):

		def withSign(flag, baseval):
			assert 0 <= baseval and baseval < 65536, 'integer overflow'
			return baseval if flag & 1 else -baseval

		nPoints = glyph.endPtsOfContours[-1] + 1
		flagSize = nPoints
		if flagSize > len(self.flagStream):
			raise TTLibError("not enough 'flagStream' data")
		flagsData = self.flagStream[:flagSize]
		self.flagStream = self.flagStream[flagSize:]
		flags = array.array('B', flagsData)

		triplets = array.array('B', self.glyphStream)
		nTriplets = len(triplets)
		assert nPoints <= nTriplets

		x = 0
		y = 0
		glyph.coordinates = getTableModule('glyf').GlyphCoordinates.zeros(nPoints)
		glyph.flags = array.array("B")
		tripletIndex = 0
		for i in range(nPoints):
			flag = flags[i]
			onCurve = not bool(flag >> 7)
			flag &= 0x7f
			if flag < 84:
				nBytes = 1
			elif flag < 120:
				nBytes = 2
			elif flag < 124:
				nBytes = 3
			else:
				nBytes = 4
			assert ((tripletIndex + nBytes) <= nTriplets)
			if flag < 10:
				dx = 0
				dy = withSign(flag, ((flag & 14) << 7) + triplets[tripletIndex])
			elif flag < 20:
				dx = withSign(flag, (((flag - 10) & 14) << 7) + triplets[tripletIndex])
				dy = 0
			elif flag < 84:
				b0 = flag - 20
				b1 = triplets[tripletIndex]
				dx = withSign(flag, 1 + (b0 & 0x30) + (b1 >> 4))
				dy = withSign(flag >> 1, 1 + ((b0 & 0x0c) << 2) + (b1 & 0x0f))
			elif flag < 120:
				b0 = flag - 84
				dx = withSign(flag, 1 + ((b0 // 12) << 8) + triplets[tripletIndex])
				dy = withSign(flag >> 1,
					1 + (((b0 % 12) >> 2) << 8) + triplets[tripletIndex + 1])
			elif flag < 124:
				b2 = triplets[tripletIndex + 1]
				dx = withSign(flag, (triplets[tripletIndex] << 4) + (b2 >> 4))
				dy = withSign(flag >> 1,
					((b2 & 0x0f) << 8) + triplets[tripletIndex + 2])
			else:
				dx = withSign(flag,
					(triplets[tripletIndex] << 8) + triplets[tripletIndex + 1])
				dy = withSign(flag >> 1,
					(triplets[tripletIndex + 2] << 8) + triplets[tripletIndex + 3])
			tripletIndex += nBytes
			x += dx
			y += dy
			glyph.coordinates[i] = (x, y)
			glyph.flags.append(int(onCurve))
		bytesConsumed = tripletIndex
		self.glyphStream = self.glyphStream[bytesConsumed:]

	def _encodeGlyph(self, glyphID):
		glyphName = self.getGlyphName(glyphID)
		glyph = self[glyphName]
		self.nContourStream += struct.pack(">h", glyph.numberOfContours)
		if glyph.numberOfContours == 0:
			return
		elif glyph.isComposite():
			self._encodeComponents(glyph)
		else:
			self._encodeCoordinates(glyph)
		self._encodeBBox(glyphID, glyph)

	def _encodeComponents(self, glyph):
		lastcomponent = len(glyph.components) - 1
		more = 1
		haveInstructions = 0
		for i in range(len(glyph.components)):
			if i == lastcomponent:
				haveInstructions = hasattr(glyph, "program")
				more = 0
			component = glyph.components[i]
			self.compositeStream += component.compile(more, haveInstructions, self)
		if haveInstructions:
			self._encodeInstructions(glyph)

	def _encodeCoordinates(self, glyph):
		lastEndPoint = -1
		for endPoint in glyph.endPtsOfContours:
			ptsOfContour = endPoint - lastEndPoint
			self.nPointsStream += pack255UShort(ptsOfContour)
			lastEndPoint = endPoint
		self._encodeTriplets(glyph)
		self._encodeInstructions(glyph)

	def _encodeInstructions(self, glyph):
		instructions = glyph.program.getBytecode()
		self.glyphStream += pack255UShort(len(instructions))
		self.instructionStream += instructions

	def _encodeBBox(self, glyphID, glyph):
		assert glyph.numberOfContours != 0, "empty glyph has no bbox"
		if not glyph.isComposite():
			# for simple glyphs, compare the encoded bounding box info with the calculated
			# values, and if they match omit the bounding box info
			currentBBox = glyph.xMin, glyph.yMin, glyph.xMax, glyph.yMax
			calculatedBBox = calcIntBounds(glyph.coordinates)
			if currentBBox == calculatedBBox:
				return
		self.bboxBitmap[glyphID >> 3] |= 0x80 >> (glyphID & 7)
		self.bboxStream += sstruct.pack(bboxFormat, glyph)

	def _encodeTriplets(self, glyph):
		assert len(glyph.coordinates) == len(glyph.flags)
		coordinates = glyph.coordinates.copy()
		coordinates.absoluteToRelative()

		flags = array.array('B')
		triplets = array.array('B')
		for i in range(len(coordinates)):
			onCurve = glyph.flags[i]
			x, y = coordinates[i]
			absX = abs(x)
			absY = abs(y)
			onCurveBit = 0 if onCurve else 128
			xSignBit = 0 if (x < 0) else 1
			ySignBit = 0 if (y < 0) else 1
			xySignBits = xSignBit + 2 * ySignBit

			if x == 0 and absY < 1280:
				flags.append(onCurveBit + ((absY & 0xf00) >> 7) + ySignBit)
				triplets.append(absY & 0xff)
			elif y == 0 and absX < 1280:
				flags.append(onCurveBit + 10 + ((absX & 0xf00) >> 7) + xSignBit)
				triplets.append(absX & 0xff)
			elif absX < 65 and absY < 65:
				flags.append(onCurveBit + 20 + ((absX - 1) & 0x30) + (((absY - 1) & 0x30) >> 2) + xySignBits)
				triplets.append((((absX - 1) & 0xf) << 4) | ((absY - 1) & 0xf))
			elif absX < 769 and absY < 769:
				flags.append(onCurveBit + 84 + 12 * (((absX - 1) & 0x300) >> 8) + (((absY - 1) & 0x300) >> 6) + xySignBits)
				triplets.append((absX - 1) & 0xff)
				triplets.append((absY - 1) & 0xff)
			elif absX < 4096 and absY < 4096:
				flags.append(onCurveBit + 120 + xySignBits)
				triplets.append(absX >> 4)
				triplets.append(((absX & 0xf) << 4) | (absY >> 8))
				triplets.append(absY & 0xff)
			else:
				flags.append(onCurveBit + 124 + xySignBits)
				triplets.append(absX >> 8)
				triplets.append(absX & 0xff)
				triplets.append(absY >> 8)
				triplets.append(absY & 0xff)

		self.flagStream += flags.tostring()
		self.glyphStream += triplets.tostring()


class WOFF2FlavorData(WOFFFlavorData):

	Flavor = 'woff2'

	def __init__(self, reader=None):
		if not haveBrotli:
			raise ImportError("No module named brotli")
		self.majorVersion = None
		self.minorVersion = None
		self.metaData = None
		self.privData = None
		if reader:
			self.majorVersion = reader.majorVersion
			self.minorVersion = reader.minorVersion
			if reader.metaLength:
				reader.file.seek(reader.metaOffset)
				rawData = reader.file.read(reader.metaLength)
				assert len(rawData) == reader.metaLength
				data = brotli.decompress(rawData)
				assert len(data) == reader.metaOrigLength
				self.metaData = data
			if reader.privLength:
				reader.file.seek(reader.privOffset)
				data = reader.file.read(reader.privLength)
				assert len(data) == reader.privLength
				self.privData = data


def unpackBase128(data):
	r""" Read one to five bytes from UIntBase128-encoded input string, and return
	a tuple containing the decoded integer plus any leftover data.

	>>> unpackBase128(b'\x3f\x00\x00') == (63, b"\x00\x00")
	True
	>>> unpackBase128(b'\x8f\xff\xff\xff\x7f')[0] == 4294967295
	True
	>>> unpackBase128(b'\x80\x80\x3f')  # doctest: +IGNORE_EXCEPTION_DETAIL
	Traceback (most recent call last):
	  File "<stdin>", line 1, in ?
	TTLibError: UIntBase128 value must not start with leading zeros
	>>> unpackBase128(b'\x8f\xff\xff\xff\xff\x7f')[0]  # doctest: +IGNORE_EXCEPTION_DETAIL
	Traceback (most recent call last):
	  File "<stdin>", line 1, in ?
	TTLibError: UIntBase128-encoded sequence is longer than 5 bytes
	>>> unpackBase128(b'\x90\x80\x80\x80\x00')[0]  # doctest: +IGNORE_EXCEPTION_DETAIL
	Traceback (most recent call last):
	  File "<stdin>", line 1, in ?
	TTLibError: UIntBase128 value exceeds 2**32-1
	"""
	if len(data) == 0:
		raise TTLibError('not enough data to unpack UIntBase128')
	result = 0
	if byteord(data[0]) == 0x80:
		# font must be rejected if UIntBase128 value starts with 0x80
		raise TTLibError('UIntBase128 value must not start with leading zeros')
	for i in range(woff2Base128MaxSize):
		if len(data) == 0:
			raise TTLibError('not enough data to unpack UIntBase128')
		code = byteord(data[0])
		data = data[1:]
		# if any of the top seven bits are set then we're about to overflow
		if result & 0xFE000000:
			raise TTLibError('UIntBase128 value exceeds 2**32-1')
		# set current value = old value times 128 bitwise-or (byte bitwise-and 127)
		result = (result << 7) | (code & 0x7f)
		# repeat until the most significant bit of byte is false
		if (code & 0x80) == 0:
			# return result plus left over data
			return result, data
	# make sure not to exceed the size bound
	raise TTLibError('UIntBase128-encoded sequence is longer than 5 bytes')


def base128Size(n):
	""" Return the length in bytes of a UIntBase128-encoded sequence with value n.

	>>> base128Size(0)
	1
	>>> base128Size(24567)
	3
	>>> base128Size(2**32-1)
	5
	"""
	assert n >= 0
	size = 1
	while n >= 128:
		size += 1
		n >>= 7
	return size


def packBase128(n):
	r""" Encode unsigned integer in range 0 to 2**32-1 (inclusive) to a string of
	bytes using UIntBase128 variable-length encoding. Produce the shortest possible
	encoding.

	>>> packBase128(63) == b"\x3f"
	True
	>>> packBase128(2**32-1) == b'\x8f\xff\xff\xff\x7f'
	True
	"""
	if n < 0 or n >= 2**32:
		raise TTLibError(
			"UIntBase128 format requires 0 <= integer <= 2**32-1")
	data = b''
	size = base128Size(n)
	for i in range(size):
		b = (n >> (7 * (size - i - 1))) & 0x7f
		if i < size - 1:
			b |= 0x80
		data += struct.pack('B', b)
	return data


def unpack255UShort(data):
	""" Read one to three bytes from 255UInt16-encoded input string, and return a
	tuple containing the decoded integer plus any leftover data.

	>>> unpack255UShort(bytechr(252))[0]
	252

	Note that some numbers (e.g. 506) can have multiple encodings:
	>>> unpack255UShort(struct.pack("BB", 254, 0))[0]
	506
	>>> unpack255UShort(struct.pack("BB", 255, 253))[0]
	506
	>>> unpack255UShort(struct.pack("BBB", 253, 1, 250))[0]
	506
	"""
	code = byteord(data[:1])
	data = data[1:]
	if code == 253:
		# read two more bytes as an unsigned short
		if len(data) < 2:
			raise TTLibError('not enough data to unpack 255UInt16')
		result, = struct.unpack(">H", data[:2])
		data = data[2:]
	elif code == 254:
		# read another byte, plus 253 * 2
		if len(data) == 0:
			raise TTLibError('not enough data to unpack 255UInt16')
		result = byteord(data[:1])
		result += 506
		data = data[1:]
	elif code == 255:
		# read another byte, plus 253
		if len(data) == 0:
			raise TTLibError('not enough data to unpack 255UInt16')
		result = byteord(data[:1])
		result += 253
		data = data[1:]
	else:
		# leave as is if lower than 253
		result = code
	# return result plus left over data
	return result, data


def pack255UShort(value):
	r""" Encode unsigned integer in range 0 to 65535 (inclusive) to a bytestring
	using 255UInt16 variable-length encoding.

	>>> pack255UShort(252) == b'\xfc'
	True
	>>> pack255UShort(506) == b'\xfe\x00'
	True
	>>> pack255UShort(762) == b'\xfd\x02\xfa'
	True
	"""
	if value < 0 or value > 0xFFFF:
		raise TTLibError(
			"255UInt16 format requires 0 <= integer <= 65535")
	if value < 253:
		return struct.pack(">B", value)
	elif value < 506:
		return struct.pack(">BB", 255, value - 253)
	elif value < 762:
		return struct.pack(">BB", 254, value - 506)
	else:
		return struct.pack(">BH", 253, value)


if __name__ == "__main__":
	import doctest
	sys.exit(doctest.testmod().failed)