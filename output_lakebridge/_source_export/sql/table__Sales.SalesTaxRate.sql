CREATE TABLE [Sales].[SalesTaxRate] (
    [SalesTaxRateID] int NOT NULL,
    [StateProvinceID] int NOT NULL,
    [TaxType] tinyint NOT NULL,
    [TaxRate] smallmoney NOT NULL,
    [Name] nvarchar(50) NOT NULL,
    [rowguid] uniqueidentifier NOT NULL,
    [ModifiedDate] datetime NOT NULL
);
