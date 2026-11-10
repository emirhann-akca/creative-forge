// OCR for every PNG in a folder with macOS Vision.
// Accurate mode with language correction OFF, so garbled AI text is reported as-is instead of being "fixed".
// Usage: ocr_vision <folder> [languages, comma separated; default en-US]
// Output: file<TAB>confidence<TAB>text<TAB>x0,y0,x1,y1 (normalized, top-left origin)
import AppKit
import Foundation
import Vision

let args = CommandLine.arguments
guard args.count >= 2 else {
    FileHandle.standardError.write("usage: ocr_vision <folder> [languages]\n".data(using: .utf8)!)
    exit(2)
}
let folder = args[1]
let languages = args.count >= 3 ? args[2].split(separator: ",").map(String.init) : ["en-US"]
let files = ((try? FileManager.default.contentsOfDirectory(atPath: folder)) ?? [])
    .filter { $0.hasSuffix(".png") }
    .sorted()

for name in files {
    let path = folder + "/" + name
    guard let image = NSImage(contentsOfFile: path),
          let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else { continue }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = false
    request.recognitionLanguages = languages
    request.minimumTextHeight = 0.008
    let handler = VNImageRequestHandler(cgImage: cg, options: [:])
    try? handler.perform([request])
    for observation in request.results ?? [] {
        guard let top = observation.topCandidates(1).first else { continue }
        let b = observation.boundingBox
        let box = String(format: "%.4f,%.4f,%.4f,%.4f", b.minX, 1 - b.maxY, b.maxX, 1 - b.minY)
        print("\(name)\t\(String(format: "%.2f", top.confidence))\t\(top.string)\t\(box)")
    }
}
