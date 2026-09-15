{{/*
Names, labels, and the two refusals.

A chart that renders with an empty image reference produces a Deployment that
never starts and a debugging session. Failing at template time costs one line
and is the whole difference.
*/}}

{{- define "sutr.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "sutr.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "sutr.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "sutr.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/name: {{ include "sutr.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "sutr.selectorLabels" -}}
app.kubernetes.io/name: {{ include "sutr.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "sutr.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "sutr.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* The image, refused rather than guessed. */}}
{{- define "sutr.image" -}}
{{- if not .Values.image.repository -}}
{{- fail "image.repository is required: set it to the registry path of your Sutr image" -}}
{{- end -}}
{{- if not .Values.image.tag -}}
{{- fail "image.tag is required: pin an exact tag or digest" -}}
{{- end -}}
{{- if eq .Values.image.tag "latest" -}}
{{- fail "image.tag must not be 'latest': two replicas of one release would be free to run different code" -}}
{{- end -}}
{{- printf "%s:%s" .Values.image.repository .Values.image.tag -}}
{{- end -}}

{{- define "sutr.secretName" -}}
{{- if not .Values.existingSecret -}}
{{- fail "existingSecret is required: create a Secret with DATABASE_URL and JWT_SECRET_KEY and name it here" -}}
{{- end -}}
{{- .Values.existingSecret -}}
{{- end -}}
