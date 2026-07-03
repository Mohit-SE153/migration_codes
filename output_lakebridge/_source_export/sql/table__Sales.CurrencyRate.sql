CREATE TABLE [Sales].[CurrencyRate] (
    [CurrencyRateID] int NOT NULL,
    [CurrencyRateDate] datetime NOT NULL,
    [FromCurrencyCode] nchar(3) NOT NULL,
    [ToCurrencyCode] nchar(3) NOT NULL,
    [AverageRate] money NOT NULL,
    [EndOfDayRate] money NOT NULL,
    [ModifiedDate] datetime NOT NULL
);
