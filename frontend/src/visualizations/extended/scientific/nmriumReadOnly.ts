/** NMRium 0.60 configuration; deliberately not upstream's editable embedded preset. */
export const nmriumReadOnlyPreferences = {
  display: {
    general: { hideGeneralSettings: true, experimentalFeatures: { display: false }, hidePanelOnLoad: true, hideLogs: true, hideWorkspaces: true, hideHelp: true, hideMaximize: true },
    panels: Object.fromEntries(['spectraPanel', 'informationPanel', 'peaksPanel', 'integralsPanel', 'rangesPanel', 'structuresPanel', 'processingsPanel', 'zonesPanel', 'summaryPanel', 'multipleSpectraAnalysisPanel', 'databasePanel', 'predictionPanel', 'automaticAssignmentPanel', 'matrixGenerationPanel', 'simulationPanel'].map((name) => [name, { display: false, open: false }])),
    toolBarButtons: { zoom: true, zoomOut: true, import: false, exportAs: false, spectraStackAlignments: false, spectraCenterAlignments: false, realImaginary: false, peakPicking: false, integral: false, zonePicking: false, slicing: false, rangePicking: false, zeroFilling: false, apodization: false, phaseCorrection: false, phaseCorrectionTwoDimensions: false, baselineCorrection: false, fft: false, fftDimension1: false, fftDimension2: false, multipleSpectraAnalysis: false, exclusionZones: false, autoRangeAndZonePicking: false },
  },
  databases: { defaultDatabase: '', data: [] }, externalAPIs: [],
  onLoadProcessing: { autoProcessing: false, filters: {} },
};
